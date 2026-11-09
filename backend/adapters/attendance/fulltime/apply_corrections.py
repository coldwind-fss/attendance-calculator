import argparse
import copy
import json
import shutil
from pathlib import Path

import openpyxl

from run_attendance import (
    _apply_attendance_status_colors,
    _attendance_day_columns,
    _calc_statistics,
    _calc_subsidy,
    _detect_month,
    _main_judge,
)
from build_fulltime_source import add_or_update_employees, last_employee_row


STAT_KEY_TO_LABEL = {
    "name": "姓名",
    "total": "合计",
    "actual_attendance": "实际出勤",
    "scheduled_attendance": "应出勤",
    "payroll_days": "计入工资核算天数",
    "normal": "正常",
    "missing_punch": "缺卡",
    "punch_correction": "补卡",
    "personal_leave": "事假",
    "sick_leave": "病假",
    "annual_leave": "年假",
    "seniority_leave": "司龄",
    "comp_leave": "调休",
    "marriage_leave": "婚假",
    "maternity_leave": "产假",
    "bereavement_leave": "丧假",
    "business_trip": "出差",
    "outing": "外出",
    "overtime_8": "加班8点",
    "overtime_9": "加班9点",
    "overtime_10": "加班10点",
    "rest": "休",
    "legal_holiday": "法定",
    "late": "迟到",
    "late_early_deduction": "迟到/早退扣款",
    "overtime_subsidy": "加班补贴",
    "travel_subsidy": "出差补贴",
    "meal_allowance": "餐补",
    "full_attendance": "全勤",
}


def parse_args():
    parser = argparse.ArgumentParser(description="应用全职考勤人工修订")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--corrections-json-file", required=True)
    parser.add_argument("--employees-json-file")
    parser.add_argument("--fast-statistic-only", action="store_true")
    return parser.parse_args()


def employee_rows(ws):
    return {
        str(ws.cell(row, 3).value or "").strip(): row
        for row in range(8, ws.max_row + 1)
        if str(ws.cell(row, 3).value or "").strip()
    }


def same_name(left, right):
    return "".join(str(left or "").split()) == "".join(str(right or "").split())


def find_employee_row(rows, name):
    for current, row in rows.items():
        if same_name(current, name):
            return row
    return None


def header_columns(ws):
    return {
        str(ws.cell(6, col).value or "").strip(): col
        for col in range(1, ws.max_column + 1)
        if str(ws.cell(6, col).value or "").strip()
    }


def remark_column(ws):
    for col in range(32, ws.max_column + 1):
        if str(ws.cell(6, col).value or "").strip() == "备注":
            return col
    return 0


def append_remark(ws, row, text):
    col = remark_column(ws)
    if not col:
        return
    current = str(ws.cell(row, col).value or "").strip()
    ws.cell(row, col, f"{current}；{text}".strip("；"))


def apply_daily(ws, row, correction):
    day = int(correction.get("day") or 0)
    if day < 1 or day > 31:
        raise ValueError("日级修订日期无效")
    day_cols = _attendance_day_columns(ws)
    col = day_cols.get(day)
    if not col:
        raise ValueError("当前工作簿中未找到对应日期列")
    old = str(ws.cell(row, col).value or "").strip()
    new = correction.get("new_value")
    if isinstance(new, dict):
        status = str(new.get("status") or "").strip()
    else:
        status = str(new or "").strip()
    if not status:
        raise ValueError("日级修订状态不能为空")
    ws.cell(row, col, status)
    append_remark(
        ws,
        row,
        f"人工修订{day}日：{old or '空'}→{status}（{correction.get('reason') or '已核实'}）",
    )


def apply_statistic(ws, row, correction, headers):
    key = str(correction.get("field_key") or "").strip()
    label = STAT_KEY_TO_LABEL.get(key, key)
    col = headers.get(label)
    if not col:
        raise ValueError(f"未找到统计字段：{key}")
    ws.cell(row, col, correction.get("new_value"))
    refresh_statistic_dependencies(ws, row, key, headers)
    append_remark(
        ws,
        row,
        f"人工调整{label}：{correction.get('reason') or '已核实'}",
    )


def apply_remark(ws, row, correction):
    """追加人工备注；备注修订本身不参与任何考勤统计计算。"""
    col = remark_column(ws)
    if not col:
        raise ValueError("当前工作簿中未找到备注列")
    text = str(correction.get("new_value") or "").strip()
    if not text:
        raise ValueError("追加备注内容不能为空")
    current = str(ws.cell(row, col).value or "").strip()
    addition = f"人工备注：{text}"
    ws.cell(row, col, f"{current}；{addition}".strip("；"))


def numeric_cell(ws, row, col):
    try:
        return float(ws.cell(row, col).value or 0)
    except (TypeError, ValueError):
        return 0.0


def refresh_statistic_dependencies(ws, row, changed_key, headers):
    """刷新人工统计修订直接影响的派生字段。"""
    overtime_keys = ("overtime_8", "overtime_9", "overtime_10")
    if changed_key not in overtime_keys:
        return
    overtime_columns = [headers.get(STAT_KEY_TO_LABEL[key]) for key in overtime_keys]
    subsidy_col = headers.get(STAT_KEY_TO_LABEL["overtime_subsidy"])
    if not subsidy_col or any(not col for col in overtime_columns):
        raise ValueError("加班补贴关联字段不完整")
    overtime_8, overtime_9, overtime_10 = [
        numeric_cell(ws, row, col) for col in overtime_columns
    ]
    subsidy = overtime_8 * 25 + overtime_9 * 45 + overtime_10 * 80
    ws.cell(row, subsidy_col, round(subsidy, 2))


def main():
    args = parse_args()
    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(input_path, output_path)
    with open(args.corrections_json_file, "r", encoding="utf-8-sig") as handle:
        corrections = json.load(handle)
    if not isinstance(corrections, list):
        raise ValueError("修订配置必须是列表")

    if args.fast_statistic_only:
        if any(item.get("target_type") != "statistic" for item in corrections):
            raise ValueError("快速修订仅支持统计字段")
        wb = openpyxl.load_workbook(output_path)
        ws = wb["全职"]
        rows = employee_rows(ws)
        headers = header_columns(ws)
        for correction in corrections:
            row = find_employee_row(rows, correction.get("employee_name"))
            if not row:
                raise ValueError(f"未找到员工：{correction.get('employee_name')}")
            apply_statistic(ws, row, correction, headers)
        _apply_attendance_status_colors(ws)
        wb.save(output_path)
        print(
            json.dumps(
                {
                    "ok": True,
                    "output": str(output_path),
                    "daily_corrections": 0,
                    "statistic_corrections": len(corrections),
                    "fast_statistic_only": True,
                },
                ensure_ascii=False,
            )
        )
        return
    employees = []
    if args.employees_json_file:
        with open(args.employees_json_file, "r", encoding="utf-8-sig") as handle:
            employees = json.load(handle)
        if not isinstance(employees, list):
            raise ValueError("新增人员配置必须是列表")

    if employees:
        wb = openpyxl.load_workbook(output_path)
        ws = wb["全职"]
        add_or_update_employees(ws, employees, last_employee_row(ws))
        wb.save(output_path)
        # 先判定考勤区；统计区在下方与人工修订合并后统一重算。
        # 不调用 run_attendance，避免对整个工作簿重复重算统计区。
        _main_judge(str(output_path))

    daily = [item for item in corrections if item.get("target_type") == "day"]
    remarks = [item for item in corrections if item.get("target_type") == "remark"]
    statistic = [
        item for item in corrections if item.get("target_type") == "statistic"
    ]

    wb = openpyxl.load_workbook(output_path)
    ws = wb["全职"]
    rows = employee_rows(ws)
    for correction in daily:
        row = find_employee_row(rows, correction.get("employee_name"))
        if not row:
            raise ValueError(f"未找到员工：{correction.get('employee_name')}")
        apply_daily(ws, row, correction)
    _apply_attendance_status_colors(ws)
    wb.save(output_path)

    # 无论本次是否有日级修订，都重算统计区：这样“重新生成草稿”会应用
    # 当前版本的统计规则（例如双休 H 列按当月实际应出勤天数折算），同时保留
    # 既有日级修订，再由下面的统计区人工修订覆盖指定字段。
    _calc_statistics(str(output_path), None)
    # 新增人员及重建草稿后，必须同步重算客服晚班 WiFi 补贴；
    # 该函数按加班时点补贴基数重建 AB 列，因此可安全重复执行。
    _calc_subsidy(str(output_path), _detect_month(str(output_path)))

    if remarks:
        wb = openpyxl.load_workbook(output_path)
        ws = wb["全职"]
        rows = employee_rows(ws)
        for correction in remarks:
            row = find_employee_row(rows, correction.get("employee_name"))
            if not row:
                raise ValueError(f"未找到员工：{correction.get('employee_name')}")
            apply_remark(ws, row, correction)
        wb.save(output_path)

    if statistic:
        wb = openpyxl.load_workbook(output_path)
        ws = wb["全职"]
        rows = employee_rows(ws)
        headers = header_columns(ws)
        for correction in statistic:
            row = find_employee_row(rows, correction.get("employee_name"))
            if not row:
                raise ValueError(f"未找到员工：{correction.get('employee_name')}")
            apply_statistic(ws, row, correction, headers)
        wb.save(output_path)

    print(
        json.dumps(
            {
                "ok": True,
                "output": str(output_path),
                "daily_corrections": len(daily),
                "remark_corrections": len(remarks),
                "statistic_corrections": len(statistic),
                "roster_employees": len(employees),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
