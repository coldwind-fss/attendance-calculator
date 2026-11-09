# -*- coding: utf-8 -*-
import argparse
import calendar
import copy
import datetime as dt
import json
import re
import sys

import openpyxl


DAY_START_COL = 33  # AG（统计区新增迟到/早退扣款后）
TEMPLATE_MAX_DAYS = 31
FIRST_EMPLOYEE_ROW = 8
STAT_START_COL = 5
STAT_END_COL = 32


SCHEDULED_BY_TYPE = {
    '双休': 21.75,
    '双球': 21.75,
    '单休': 25,
    '月休四天': 25,
    '单双休': 24,
    '单双休客服': 24,
    '单体': 25,
}


def parse_args():
    parser = argparse.ArgumentParser(description='构建全职考勤三Sheet源表')
    parser.add_argument('--template', required=True)
    parser.add_argument('--monthly', required=True)
    parser.add_argument('--raw', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--month', type=int)
    parser.add_argument('--year', type=int)
    parser.add_argument('--employees-json', default='[]')
    parser.add_argument('--employees-json-file')
    return parser.parse_args()


def main():
    args = parse_args()
    if args.employees_json_file:
        with open(args.employees_json_file, 'r', encoding='utf-8-sig') as fh:
            employees = json.load(fh)
    else:
        employees = json.loads(args.employees_json or '[]')

    src_monthly_wb = openpyxl.load_workbook(args.monthly)
    src_raw_wb = openpyxl.load_workbook(args.raw)
    monthly_ws = src_monthly_wb.worksheets[0]
    raw_ws = src_raw_wb.worksheets[0]
    validate_source_sheet(monthly_ws, '月度汇总', '月度汇总 Excel')
    validate_source_sheet(raw_ws, '原始记录', '原始记录 Excel')

    year, month = detect_year_month(monthly_ws, args.year, args.month)
    days_count = calendar.monthrange(year, month)[1]

    wb = openpyxl.load_workbook(args.template)

    replace_sheet(wb, monthly_ws, '月度数据', 1)
    replace_sheet(wb, raw_ws, '原始数据', 2)
    ws_full = wb['全职']
    ws_full.cell(3, 3, year)
    ws_full.cell(3, 4, month)

    # 固定模板只作为历史人员和格式基准；当月源表可能包含新增或调动人员，
    # 这些人员不能因为没有提前写入模板而被丢弃。配置中的入职日期/出勤类型
    # 仍然优先用于填充新增人员的业务字段。
    employees, source_employee_count = merge_source_employees(employees, monthly_ws)
    adjust_fulltime_sheet(ws_full, year, month, days_count, employees)
    wb.save(args.output)
    print(json.dumps({
        'ok': True,
        'output': args.output,
        'year': year,
        'month': month,
        'days': days_count,
        'employees_added': len([e for e in employees if e.get('name')]),
        'source_employee_count': source_employee_count
    }, ensure_ascii=False))


def detect_year_month(ws, year=None, month=None):
    if year and month:
        return int(year), int(month)
    text = str(ws.cell(1, 1).value or '')
    m = re.search(r'(\d{4})-(\d{2})-\d{2}', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    today = dt.date.today()
    return year or today.year, month or today.month


def validate_source_sheet(ws, expected_title, field_label):
    title = str(ws.cell(1, 1).value or '').strip()
    if expected_title not in title:
        raise ValueError(
            f'{field_label} 文件不匹配：首个工作表应为“{expected_title}”，'
            f'当前读取到“{title or "空表头"}”。请检查两个上传文件是否放反或重复选择。'
        )


def replace_sheet(target_wb, source_ws, target_name, target_index):
    if target_name in target_wb.sheetnames:
        old = target_wb[target_name]
        target_wb.remove(old)
    ws = target_wb.create_sheet(target_name, target_index)
    copy_sheet_content(source_ws, ws)


def copy_sheet_content(src, dst):
    dst.sheet_format = copy.copy(src.sheet_format)
    dst.sheet_properties = copy.copy(src.sheet_properties)
    dst.page_margins = copy.copy(src.page_margins)
    dst.page_setup = copy.copy(src.page_setup)
    dst.freeze_panes = src.freeze_panes
    for key, dim in src.column_dimensions.items():
        dst.column_dimensions[key].width = dim.width
        dst.column_dimensions[key].hidden = dim.hidden
    for key, dim in src.row_dimensions.items():
        dst.row_dimensions[key].height = dim.height
        dst.row_dimensions[key].hidden = dim.hidden
    for row in src.iter_rows():
        for cell in row:
            out = dst[cell.coordinate]
            if out.__class__.__name__ == 'MergedCell':
                continue
            out.value = cell.value
            if cell.has_style:
                out._style = copy.copy(cell._style)
            if cell.number_format:
                out.number_format = cell.number_format
            if cell.alignment:
                out.alignment = copy.copy(cell.alignment)
            if cell.fill:
                out.fill = copy.copy(cell.fill)
            if cell.font:
                out.font = copy.copy(cell.font)
            if cell.border:
                out.border = copy.copy(cell.border)
    for merged in src.merged_cells.ranges:
        dst.merge_cells(str(merged))


def adjust_fulltime_sheet(ws, year, month, days_count, employees):
    if days_count < TEMPLATE_MAX_DAYS:
        delete_start = DAY_START_COL + days_count
        ws.delete_cols(delete_start, TEMPLATE_MAX_DAYS - days_count)

    remark_col = DAY_START_COL + days_count
    fix_merged_ranges(ws, days_count, remark_col)

    for day in range(1, days_count + 1):
        col = DAY_START_COL + day - 1
        ws.cell(6, col, day)
        ws.cell(7, col, weekday_cn(year, month, day))

    ws.cell(6, remark_col, '备注')
    ws.cell(7, remark_col, None)

    last_row = last_employee_row(ws)
    add_or_update_employees(ws, employees, last_row)
    last_row = last_employee_row(ws)

    for row in range(FIRST_EMPLOYEE_ROW, last_row + 1):
        if not str(ws.cell(row, 3).value or '').strip():
            continue
        ws.cell(row, 2, '=ROW()-7')
        for col in range(STAT_START_COL, STAT_END_COL + 1):
            if col == 7:
                continue
            ws.cell(row, col).value = None
        for col in range(DAY_START_COL, remark_col + 1):
            ws.cell(row, col).value = None
    write_total_row(ws, last_row)


def fix_merged_ranges(ws, days_count, remark_col):
    wanted = {
        f'{openpyxl.utils.get_column_letter(DAY_START_COL)}5:{openpyxl.utils.get_column_letter(remark_col)}5',
        f'{openpyxl.utils.get_column_letter(remark_col)}6:{openpyxl.utils.get_column_letter(remark_col)}7',
    }
    for merged in list(ws.merged_cells.ranges):
        if merged.min_col >= DAY_START_COL and merged.min_row in (5, 6):
            try:
                ws.unmerge_cells(str(merged))
            except KeyError:
                pass
    for rng in wanted:
        try:
            ws.merge_cells(rng)
        except ValueError:
            pass


def add_or_update_employees(ws, employees, last_row):
    by_name = {
        str(ws.cell(row, 3).value or '').strip(): row
        for row in range(FIRST_EMPLOYEE_ROW, last_row + 1)
        if str(ws.cell(row, 3).value or '').strip()
    }
    row = last_row
    for employee in employees:
        name = str(employee.get('name') or '').strip()
        if not name:
            continue
        target_row = by_name.get(name)
        if not target_row:
            row += 1
            target_row = row
            copy_employee_row_style(ws, target_row - 1, target_row)
            ws.cell(target_row, 3, name)
            by_name[name] = target_row
        if employee.get('entry_date'):
            ws.cell(target_row, 4, normalize_entry_date(employee.get('entry_date')))
        scheduled = scheduled_days(employee)
        if scheduled is not None:
            ws.cell(target_row, 7, scheduled)


def normalize_source_employee_name(value):
    """将月度汇总中的“昵称-姓名（离职）”归一为结果表使用的姓名。"""
    text = str(value or '').strip()
    if not text:
        return ''
    text = re.sub(r'[（(]\s*离职\s*[）)]\s*$', '', text).strip()
    if '-' in text:
        text = text.rsplit('-', 1)[1].strip()
    return re.sub(r'[（(]\s*离职\s*[）)]\s*$', '', text).strip()


def merge_source_employees(configured_employees, monthly_ws):
    """合并员工配置和当月源表人员，确保源表中的人员不会被固定模板过滤。"""
    merged = []
    by_name = {}
    for employee in configured_employees or []:
        item = dict(employee or {})
        name = str(item.get('name') or '').strip()
        if not name:
            continue
        item['name'] = name
        if name in by_name:
            merged[by_name[name]].update(item)
        else:
            by_name[name] = len(merged)
            merged.append(item)

    source_names = set()
    for row in range(FIRST_EMPLOYEE_ROW - 3, monthly_ws.max_row + 1):
        name = normalize_source_employee_name(monthly_ws.cell(row, 1).value)
        if name and name not in source_names:
            source_names.add(name)
            if name not in by_name:
                by_name[name] = len(merged)
                merged.append({'name': name})
    return merged, len(source_names)


def write_total_row(ws, last_employee):
    total_row = last_employee + 1
    copy_employee_row_style(ws, last_employee, total_row)
    for col in range(1, ws.max_column + 1):
        ws.cell(total_row, col).value = None
    for col in range(6, STAT_END_COL + 1):
        letter = openpyxl.utils.get_column_letter(col)
        ws.cell(total_row, col, f'=SUM({letter}{FIRST_EMPLOYEE_ROW}:{letter}{last_employee})')


def copy_employee_row_style(ws, source_row, target_row):
    ws.row_dimensions[target_row].height = ws.row_dimensions[source_row].height
    max_col = max(ws.max_column, DAY_START_COL + TEMPLATE_MAX_DAYS)
    for col in range(1, max_col + 1):
        src = ws.cell(source_row, col)
        dst = ws.cell(target_row, col)
        if src.has_style:
            dst._style = copy.copy(src._style)
        dst.number_format = src.number_format
        dst.alignment = copy.copy(src.alignment)
        dst.fill = copy.copy(src.fill)
        dst.font = copy.copy(src.font)
        dst.border = copy.copy(src.border)


def scheduled_days(employee):
    if employee.get('scheduled_days') not in (None, ''):
        try:
            return float(employee.get('scheduled_days'))
        except Exception:
            pass
    attendance_type = str(employee.get('attendance_type') or employee.get('type') or '').strip()
    return SCHEDULED_BY_TYPE.get(attendance_type)


def normalize_entry_date(value):
    if isinstance(value, dt.datetime):
        return value
    text = str(value).strip().replace('/', '-')
    try:
        return dt.datetime.strptime(text[:10], '%Y-%m-%d')
    except Exception:
        return value


def weekday_cn(year, month, day):
    return '一二三四五六日'[dt.date(year, month, day).weekday()]


def last_employee_row(ws):
    last = FIRST_EMPLOYEE_ROW - 1
    for row in range(FIRST_EMPLOYEE_ROW, ws.max_row + 1):
        if str(ws.cell(row, 3).value or '').strip():
            last = row
    return last


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise
