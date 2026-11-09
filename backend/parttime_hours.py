"""Match the normalized attendance rows and apply the existing hour rules."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import floor
from uuid import uuid4


PRIMARY_STORES = {"official", "sports", "tmall"}
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
EPOCH = datetime(1970, 1, 1)


def round_hours(value, digits):
    scaled = value * 10**digits
    integer = floor(scaled)
    return (integer + (scaled - integer >= 0.5)) / 10**digits


def sum_hours(values):
    total = 0
    for value in values:
        total += value
    return total


def fixed_hours(value):
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def milliseconds(value):
    date = datetime.fromisoformat(value)
    delta = date - EPOCH
    return delta.days * DAY_MS + delta.seconds * 1000 + delta.microseconds // 1000


def schedule_interval(row):
    if not row["start"] or not row["end"]:
        return None
    midnight = milliseconds(row["date"])
    times = [tuple(map(int, value.split(":"))) for value in (row["start"], row["end"])]
    start, end = (midnight + hours * HOUR_MS + minutes * 60_000 for hours, minutes in times)
    return start, end if end > start else end + DAY_MS


def source_interval(row):
    if not row["start"] or not row["end"]:
        return None
    return milliseconds(row["start"]), milliseconds(row["end"])


def overlap(left, right):
    return max(0, min(left[1], right[1]) - max(left[0], right[0])) if left and right else 0


@dataclass
class Match:
    row_id: str
    start: int
    end: int
    allocated_ms: float


@dataclass
class Allocation:
    hours: float = 0
    session_ids: list[str] = field(default_factory=list)


def allocate_session(matches, start, end):
    # Each role gets an independent share of the session. Gaps between two
    # assignments are split, and overlapping assignments are scaled together.
    cursor = start
    previous = None
    for match in matches:
        gap = max(0, match.start - cursor)
        if previous is None:
            match.allocated_ms += gap
        elif gap:
            previous.allocated_ms += gap / 2
            match.allocated_ms += gap / 2
        cursor = max(cursor, match.end)
        previous = match
    previous.allocated_ms += end - cursor
    total = sum_hours(match.allocated_ms for match in matches)
    if total > end - start:
        ratio = (end - start) / total
        for match in matches:
            match.allocated_ms *= ratio


def allocate_backend(schedule, backend):
    candidates = defaultdict(list)
    for row in schedule:
        interval = schedule_interval(row)
        if interval and row["person"] and row["role"] and not row.get("exclude_from_payroll"):
            candidates[(row["store_id"], row["date"], row["role"])].append((row["id"], *interval))

    allocations = defaultdict(Allocation)
    for session in backend:
        if session["hours"] <= 0:
            continue
        start = milliseconds(session["start"])
        # The old parser rounds hours separately from the displayed end time.
        end = int(start + session["hours"] * HOUR_MS)
        for role in ("主播", "中控"):
            rows = candidates[(session["store_id"], session["date"], role)]
            matches = []
            for row_id, row_start, row_end in sorted(rows, key=lambda row: row[1]):
                clipped_start, clipped_end = max(start, row_start), min(end, row_end)
                if clipped_start < clipped_end:
                    matches.append(Match(row_id, clipped_start, clipped_end, clipped_end - clipped_start))
            if not matches:
                continue
            allocate_session(matches, start, end)
            for match in matches:
                allocation = allocations[match.row_id]
                allocation.hours += match.allocated_ms / HOUR_MS
                if session["id"] not in allocation.session_ids:
                    allocation.session_ids.append(session["id"])
    return allocations


def final_hours(row, mode, threshold):
    planned, backend = row["planned_hours"], row["backend_hours"]
    if mode == "backend_allocation":
        return planned if backend < planned and planned - backend <= threshold else backend
    if planned <= 0:
        return 0
    extra = max(backend - planned, 0)
    if row["store_id"] == "sports":
        return planned + min(extra, 0.5)
    if row["store_id"] != "official" and 0 < extra <= threshold:
        return planned + extra
    return planned


def mark_warnings(row, threshold):
    backend_diff = abs(row["schedule_vs_backend_diff"])
    operation_diff = abs(row["operation_vs_schedule_diff"] or 0)
    backend_missing = row["backend_hours"] <= 0
    operation_missing = row["has_operation_source"] and row["operation_hours"] <= 0
    details = []
    if backend_missing:
        details.append("未匹配到后台真实直播时段")
    if backend_diff > threshold:
        details.append(f"后台与排班相差 {fixed_hours(backend_diff)} 小时")
    if operation_diff > threshold:
        details.append(f"经营表与排班相差 {fixed_hours(operation_diff)} 小时")
    if operation_missing:
        details.append("未匹配到经营表人员时长")
    if backend_diff > threshold or operation_diff > threshold:
        warning = "差异过大"
    elif backend_missing:
        warning = "缺后台"
    elif operation_missing:
        warning = "缺经营数据"
    else:
        warning = "正常"
    if backend_missing:
        status = "未匹配后台"
    elif warning != "正常":
        status = warning
    else:
        status = "后台少于排班" if row["backend_hours"] + 0.05 < row["planned_hours"] else "已匹配"
    row.update(warning_level=warning, warning_detail="；".join(details) or "正常", status=status)


def supplemental_row(source, operation_source, **fields):
    return {
        "id": uuid4().hex, "date": source["date"], "store_id": source["store_id"],
        "store_name": source["store_name"], "planned_hours": 0, "partner": "",
        "exclude_from_payroll": True, "has_operation_source": operation_source,
        "warning_level": "缺排班", "status": "缺排班", **fields,
        "actual_hours": fields["final_hours"], "diff_hours": fields["schedule_vs_backend_diff"],
    }


def match_hours(schedule, backend, operations, *, mode, warning_hours):
    threshold = round_hours(warning_hours, 2)
    has_operations = bool(operations)
    allocations = allocate_backend(schedule, backend)
    operations_by_person = defaultdict(list)
    backend_by_day = defaultdict(list)
    for row in operations:
        operations_by_person[(row["store_id"], row["date"], row["person"])].append((row, source_interval(row)))
    for row in backend:
        backend_by_day[(row["store_id"], row["date"])].append((row, source_interval(row)))

    matched = []
    roles = {}
    intervals_by_person = defaultdict(list)
    for source in schedule:
        row = dict(source)
        key = row["store_id"], row["date"], row["person"]
        interval = schedule_interval(row)
        intervals_by_person[key].append(interval)
        roles.setdefault(row["person"], row["role"])
        allocation = allocations[row["id"]]
        operation_rows = [item for item, when in operations_by_person[key] if overlap(interval, when)]
        backend_hours = round_hours(allocation.hours, 3)
        operation_hours = round_hours(sum_hours(item["operation_hours"] for item in operation_rows), 3)
        difference = round_hours(backend_hours - row["planned_hours"], 3)
        row.update(backend_hours=backend_hours, operation_hours=operation_hours, schedule_vs_backend_diff=difference,
                   operation_vs_backend_diff=round_hours(backend_hours - operation_hours, 3) if operation_hours > 0 else "",
                   operation_vs_schedule_diff=round_hours(operation_hours - row["planned_hours"], 3) if operation_hours > 0 else "",
                   diff_hours=difference, has_operation_source=has_operations, matched_backend_ids=allocation.session_ids,
                   matched_operation_ids=[item["id"] for item in operation_rows])
        row["actual_hours"] = row["final_hours"] = round_hours(final_hours(row, mode, threshold), 3)
        mark_warnings(row, threshold)
        matched.append(row)

    for item in operations:
        key = item["store_id"], item["date"], item["person"]
        interval = source_interval(item)
        if any(overlap(when, interval) for when in intervals_by_person[key]):
            continue
        backend_hours = 0
        session_ids = []
        for session, when in backend_by_day[key[:2]]:
            covered = overlap(interval, when)
            if covered:
                backend_hours += covered / HOUR_MS
                session_ids.append(session["id"])
        operation_hours = item["operation_hours"]
        start, end = [(EPOCH + timedelta(milliseconds=milliseconds(item[key]))).strftime("%H:%M") if item[key] else ""
                      for key in ("start", "end")]
        row = supplemental_row(
            item, has_operations, person=item["person"], role=roles.get(item["person"]) or "主播", start=start, end=end,
            backend_hours=round_hours(backend_hours, 3), operation_hours=operation_hours,
            schedule_vs_backend_diff=round_hours(backend_hours, 3),
            operation_vs_backend_diff=round_hours(backend_hours - operation_hours, 3),
            operation_vs_schedule_diff=round_hours(operation_hours, 3),
            final_hours=round_hours(max(backend_hours, operation_hours), 3),
            band="经营表补充", source_cell=f"{item['source_sheet']}!{item['source_row']}", raw_value=item["person"],
            from_operation_only=True, matched_backend_ids=session_ids, matched_operation_ids=[item["id"]],
            warning_detail="经营表人员时长未匹配到排班，已排除最终结算/兼职工资",
        )
        matched.append(row)
        intervals_by_person[key].append(schedule_interval(row))

    assigned = {session_id for row in matched for session_id in row["matched_backend_ids"]}
    for session in backend:
        if session["id"] in assigned or session["store_id"] not in PRIMARY_STORES:
            continue
        matched.append(supplemental_row(
            session, has_operations, role="", person="", start="", end="", backend_hours=session["hours"],
            operation_hours=0, schedule_vs_backend_diff=session["hours"], operation_vs_backend_diff="",
            operation_vs_schedule_diff="", final_hours=session["hours"], band="后台补充",
            source_cell=f"后台第 {session['source_row']} 行", raw_value=session.get("title") or "", from_backend_only=True,
            matched_backend_ids=[session["id"]], matched_operation_ids=[],
            warning_detail="后台真实场次未匹配到排班或经营人员记录",
        ))
    return matched
