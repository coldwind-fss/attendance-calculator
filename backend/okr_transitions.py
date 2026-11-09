"""Pure OKR changes executed after the legacy link and identity checks."""

import json
import sys
from copy import deepcopy


WEEKS = ("第一周", "第二周", "第三周", "第四周", "第五周")
WEEKLY_STATES = {"target_submitted", "target_manager_approved", "target_bp_approved",
                 "target_confirmed", "self_review_invited"}


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def text(value):
    return str(value or "").strip()


def array(value):
    return value if isinstance(value, list) else []


def weight(value, label):
    candidate = number(value)
    if not candidate.is_integer() or not 1 <= candidate <= 100:
        raise ValueError(f"{label}权重必须为1至100的整数")
    return int(candidate)


def objectives(raw, label, *, weekly=False):
    items = array(raw)
    if weekly:
        items = [item for item in items if item and (text(item.get("title"))
                 or any(kr and text(kr.get("result")) for kr in array(item.get("keyResults"))))]
    if len(items) > 10 or (not weekly and not items):
        raise ValueError(f"{label}需填写1至10项O")
    result = []
    for oi, item in enumerate(items, 1):
        title = text(item.get("title"))
        if not title:
            raise ValueError(f"{label}第{oi}项O不能为空")
        amount = weight(item.get("weight"), f"{label}第{oi}项O")
        key_results = array(item.get("keyResults"))
        if weekly:
            key_results = [kr for kr in key_results if kr and text(kr.get("result"))]
        if not 1 <= len(key_results) <= 15:
            raise ValueError(f"{label}第{oi}项O需填写1至15条KR")
        normalized = []
        for ki, kr in enumerate(key_results, 1):
            outcome = text(kr.get("result"))
            if not outcome:
                raise ValueError(f"{label}第{oi}项O的第{ki}条KR不能为空")
            normalized.append({"seq": ki, "result": outcome, "weight": weight(kr.get("weight"), f"{label}O{oi} KR{ki}")})
        if sum(kr["weight"] for kr in normalized) != amount:
            raise ValueError(f"{label}第{oi}项O下的KR权重合计必须等于O权重{amount}%")
        result.append({"seq": oi, "title": title, "weight": amount, "keyResults": normalized})
    if result and sum(item["weight"] for item in result) != 100:
        raise ValueError(f"{label}O权重合计必须为100%")
    return result


def weekly_objectives(raw):
    source = array(raw)
    output = []
    for index, label in enumerate(WEEKS):
        week = next((item for item in source if item and number(item.get("week")) == index + 1), None)
        if week is None:
            week = (source[index] or {}) if index < len(source) else {}
        output.append({"week": index + 1, "label": label, "objectives": objectives(week.get("objectives"), label, weekly=True)})
    return output


def require_state(record, *states):
    if record["status"] not in states:
        raise ValueError("当前OKR流程节点不支持该操作，请刷新后重试")


def notice(page, recipient, title, body):
    return {"page": page, "recipient": recipient, "title": title, "body": body}


def prepare_transition(action, record, data, now, weekly, base_weekly, coefficients):
    employee = record["emp"]
    name, month = employee["name"], record["month"]
    notification = None

    if action == "okr-weekly-save":
        require_state(record, *WEEKLY_STATES)
        updated = weekly_objectives(data.get("weeklyObjectives"))
        patch = {"operationalWeeklyObjectives": updated, "weeklyUpdatedAt": now,
                 "weeklyUpdates": array(record.get("weeklyUpdates")) + [{"updatedAt": now, "before": weekly, "after": updated}]}
        message = "周度O/KR已保存，现有OKR流程节点未改变。"
    elif action in ("okr-target-submit", "okr-target-manager", "okr-target-bp"):
        before, status, page, recipient, title, body, message = {
            "okr-target-submit": (("invited", "target_rejected"), "target_submitted", "target-manager", "directMgr",
                                  f"直属上级确认OKR目标 - {month}{name}", f"{name}已提交OKR目标，请确认或直接调整。",
                                  "已通知直属上级确认OKR目标。"),
            "okr-target-manager": (("target_submitted",), "target_manager_approved", "target-bp", "hrbp",
                                   f"BP确认OKR目标 - {month}{name}", "直属上级已确认OKR目标，请BP继续核对或调整。",
                                   "上级已确认，已发送BP。"),
            "okr-target-bp": (("target_manager_approved",), "target_bp_approved", "target-confirm", "name",
                              f"员工确认OKR目标 - {month}", "BP已确认你的OKR目标，请本人确认。",
                              "BP已确认，已发送员工确认OKR目标。"),
        }[action]
        require_state(record, *before)
        target = {"objectives": objectives(data.get("objectives"), "月度"),
                  "weeklyObjectives": weekly_objectives(data.get("weeklyObjectives"))}
        patch = target | {"status": status}
        if action == "okr-target-submit":
            patch.update(submittedAt=now, reviewReason="")
        else:
            previous = {"objectives": array(record.get("objectives")), "weeklyObjectives": base_weekly}
            if json.dumps(previous, ensure_ascii=False) != json.dumps(target, ensure_ascii=False):
                manager = action == "okr-target-manager"
                patch["adjustments"] = array(record.get("adjustments")) + [{
                    "actorRole": "直属上级" if manager else "BP",
                    "actorName": employee.get("directMgr" if manager else "hrbp", ""),
                    "adjustedAt": now, "before": previous, "after": target,
                }]
        notification = notice(page, employee.get(recipient, ""), title, body)
    elif action == "okr-self-review":
        require_state(record, "self_review_invited")
        target = deepcopy(record["objectives"])
        completions = {}
        for item in array(data.get("completions")):
            oi, ki = item.get("objectiveIndex"), item.get("krIndex")
            if type(oi) in (int, float) and type(ki) in (int, float):
                completions.setdefault((oi, ki), text(item.get("completion")))
        for oi, objective in enumerate(target):
            for ki, kr in enumerate(objective["keyResults"]):
                value = completions.get((oi, ki))
                if not value:
                    raise ValueError(f"请填写O{oi + 1} KR{ki + 1}的完成情况")
                kr["completion"] = value
        summary = text(data.get("summary"))
        if not summary:
            raise ValueError("请填写OKR月度总结")
        patch = {"objectives": target, "summary": summary, "status": "self_reviewed"}
        notification = notice("manager-grade", employee.get("directMgr", ""), f"直属上级评定OKR等级 - {month}{name}",
                              f"{name}已提交OKR完成情况，请直属上级评定等级。")
        message = "OKR月度结果已提交直属上级。"
    else:
        grade = data.get("grade")
        if grade not in coefficients:
            raise ValueError("无效的OKR等级")
        comment = text(data.get("comment"))
        if action == "okr-manager-grade":
            require_state(record, "self_reviewed")
            patch = {"managerGrade": grade, "managerComment": comment, "status": "manager_graded"}
            notification = notice("bp-grade", employee.get("hrbp", ""), f"BP核定OKR等级 - {month}{name}",
                                  "直属上级已评定OKR等级，请BP核定最终等级。")
            message = "上级等级已提交BP核定。"
        else:
            require_state(record, "manager_graded")
            coefficient = coefficients[grade]
            patch = {"bpGrade": grade, "bpComment": comment, "finalCoefficient": coefficient,
                     "status": "completed", "completedAt": now}
            notification = notice("result-confirm", name, f"员工确认OKR结果 - {month}",
                                  f"BP已核定你的OKR最终等级为{grade}，独立奖金发放系数为{coefficient}%，请本人确认结果。")
            message = f"BP最终等级已核定，独立奖金发放系数为{coefficient}%。"

    return {"patch": patch, "message": message, "notification": notification}


if __name__ == "__main__":
    try:
        response = {"result": prepare_transition(**json.load(sys.stdin))}
    except ValueError as error:
        response = {"error": str(error)}
    except (TypeError, KeyError, AttributeError):
        response = {"error": "OKR请求字段无效"}
    print(json.dumps(response, ensure_ascii=False))
