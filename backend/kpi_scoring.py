"""Authoritative KPI totals for the already-authorized score submissions."""

import json
import sys


MISSING = object()
GRADE_THRESHOLDS = ((110, "A-"), (100, "B+"), (90, "B"), (80, "B-"), (70, "C"))


def numeric(value):
    if value is None or isinstance(value, str) and not value.strip():
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return float("nan")


def calculate_kpi_score(kind, units, details):
    targets = {numeric(item["seq"]): numeric(item["max"]) for item in units}
    scores = {}
    field = "selfScore" if kind == "self" else "score"
    for item in details:
        if kind == "self" and not str(item.get("completion") or "").strip():
            raise ValueError("Please complete the completion notes before scoring")
        seq = numeric(item.get("seq", MISSING))
        if seq not in targets or seq in scores:
            raise ValueError("Invalid or duplicate KPI item")
        score = numeric(item.get(field, MISSING))
        if not score.is_integer() or score < 0 or not 0 < targets[seq] < float("inf"):
            raise ValueError(f"{kind.capitalize()} score for KPI {seq:g} must be a non-negative integer")
        scores[seq] = int(score)
    if len(scores) != len(targets):
        raise ValueError("Please score every KPI item")
    total = sum(scores.values())
    if not 0 <= total <= 120:
        raise ValueError("KPI total must be an integer between 0 and 120")
    return {"total": total, "grade": next((grade for threshold, grade in GRADE_THRESHOLDS if total > threshold), "D"),
            "scores": {str(int(seq)) if seq.is_integer() else str(seq): score for seq, score in scores.items()}}


if __name__ == "__main__":
    try:
        response = {"result": calculate_kpi_score(**json.load(sys.stdin))}
    except ValueError as error:
        response = {"error": str(error)}
    except (TypeError, KeyError, AttributeError):
        response = {"error": "KPI请求字段无效"}
    print(json.dumps(response, ensure_ascii=False))
