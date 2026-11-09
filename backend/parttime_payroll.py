"""Payroll amounts and settlement rows for the normalized attendance workbench."""

from math import ceil

from .parttime_hours import PRIMARY_STORES, round_hours, sum_hours


HOUR_COLUMNS = ("official", "sports", "sports_xhs", "sports_video", "sports_dou2", "tmall", "shooting", "total_hours")
PAY_CHANNELS = ("official", "sports_dou2", "sports_video", "sports_xhs", "tmall", "shooting")
ROLE_ORDER = {"主播": 1, "中控": 2}


def rate_for(policy, person, role):
    return policy["person_rates"].get(person, policy["role_rates"].get(role, 0))


def payroll_groups(schedule, policy):
    grouped = {}
    for row in schedule:
        if row.get("exclude_from_payroll"):
            continue
        key = row["store_id"], row["role"], row["person"]
        group = grouped.setdefault(key, {column: row[column] for column in ("store_id", "store_name", "role", "person")}
                                   | {"planned_hours": 0, "actual_hours": 0})
        group["planned_hours"] += row["planned_hours"]
        group["actual_hours"] += row["actual_hours"]
    order = {value: index for index, value in enumerate(policy["payroll_group_order"])}
    result = []
    for group in grouped.values():
        rate = rate_for(policy, group["person"], group["role"])
        result.append(group | {"planned_hours": round_hours(group["planned_hours"], 2),
                               "actual_hours": round_hours(group["actual_hours"], 2),
                               "rate": rate, "amount": round_hours(group["actual_hours"] * rate, 2)})
    return sorted(result, key=lambda row: order[row["store_name"] + row["role"] + row["person"]])


def reference_order(reference, policy):
    positions = {(row["role"], row["alias"]): index for index, row in enumerate(reference)}
    aliases = {alias: index for index, alias in enumerate(policy["alias_order"])}

    def rank(row):
        position = positions.get((row["role"], row["person"]))
        if position is not None:
            return (0, position, 0)
        return (1, ROLE_ORDER.get(row["role"], 9), aliases[row["alias"]])

    return rank


def settlement_rows(payroll, policy, reference):
    names = {(row["role"], row["alias"]): row.get("name") or row["alias"] for row in reference}
    people = {}
    for item in payroll:
        key = item["role"], item["person"]
        person = item["person"]
        if not all(key):
            continue
        row = people.setdefault(key, {
            "role": item["role"], "person": person, "alias": policy["display_aliases"].get(person, person),
            "name": names.get(key, policy["real_names"].get(person, person)), "remark": "",
            **dict.fromkeys(HOUR_COLUMNS, 0),
        })
        if item["store_id"] in PRIMARY_STORES:
            row[item["store_id"]] += item["actual_hours"]

    for target in policy["protection_targets"]:
        row = next((row for row in people.values() if target["person"] in (row["person"], row["alias"])), None)
        if row is None:
            continue
        for channel in ("official", "sports", "tmall", "shooting"):
            if channel in target:
                protected = max(row[channel], target[channel])
                if channel in ("sports", "tmall") and protected - target[channel] > 0.25:
                    protected = target[channel]
                row[channel] = protected

    for row in people.values():
        if row["person"] in policy["sports_xhs_only"] or row["alias"] in policy["sports_xhs_only"]:
            row["sports_xhs"] = row["sports"]
        else:
            third = ceil(row["sports"] / 3 * 10_000) / 10_000
            for channel in ("sports_xhs", "sports_video", "sports_dou2"):
                row[channel] = third
        row["total_hours"] = sum_hours(row[channel] for channel in ("official", "sports", "tmall", "shooting"))
        for channel in HOUR_COLUMNS:
            row[channel] = round_hours(row[channel], 4 if channel.startswith("sports_") else 2) or ""
    return sorted(people.values(), key=reference_order(reference, policy))


def payroll_rows(settlement, policy, reference):
    names = {(row["role"], row["alias"]): row.get("name") or row["alias"] for row in reference}
    rates = {(row["role"], row["alias"]): row.get("rate") or rate_for(policy, row["alias"], row["role"]) for row in reference}
    result = []
    for index, source in enumerate(sorted(settlement, key=reference_order(reference, policy)), 1):
        key = source["role"], source["person"]
        rate = rates.get(key) or rate_for(policy, source["person"], source["role"])
        row = {"role": source["role"], "index": index, "alias": source["alias"], "name": names.get(key, source["name"]),
               "rate": rate, "remark": source["remark"]}
        total_hours = 0
        for channel in PAY_CHANNELS:
            hours = source[channel] or 0
            total_hours += hours
            amount = round_hours(hours * rate, 2) or ""
            row.update({f"{channel}_hours": hours or "", f"{channel}_amount": amount,
                        f"{channel}_tax": "", f"{channel}_net": amount})
        total = round_hours(total_hours * rate, 2)
        row.update(total_hours=round_hours(total_hours, 2), total_amount=total, total_tax="", total_net=total)
        result.append(row)
    return result


def calculate_settlement(schedule, policy, *, attendance=None, settlement=None):
    computed = payroll_groups(schedule, policy)
    primary = [row for row in schedule if row["role"] and row["person"] and row["store_id"] in PRIMARY_STORES
               and not row.get("from_operation_only") and not row.get("exclude_from_payroll")]
    summary = payroll_groups(primary, policy)
    settlement_reference = (settlement or {}).get("people") or policy["settlement_order"]
    attendance_reference = (attendance or {}).get("rows")
    if attendance_reference:
        payroll_reference = [row for row in attendance_reference if row["role"] != "合计" and row["alias"] != "合计"]
    else:
        payroll_reference = policy["payroll_order"]
    system_settlement = settlement_rows(summary, policy, settlement_reference)
    return {"computedPayroll": computed, "systemSummaryPayroll": summary, "systemSettlement": system_settlement,
            "systemPayroll": payroll_rows(system_settlement, policy, payroll_reference)}
