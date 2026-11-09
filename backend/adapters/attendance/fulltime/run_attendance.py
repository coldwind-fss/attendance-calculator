# -*- coding: utf-8 -*-
"""
全职考勤自动判定 — 合并版
调用：
    from run_attendance import run_attendance
    run_attendance("考勤文件.xlsx", config={...}, ningbo_list=[...], month=5)
"""
import argparse
import calendar
import datetime
import json
import os
import openpyxl, re, time
import shutil
from pathlib import Path
from openpyxl.styles import PatternFill

from patch_template_late_early import patch_template as _patch_late_early_template


_policy_file = os.getenv('HR_COMPANY_POLICY')
_POLICY = json.loads(Path(_policy_file).read_text(encoding='utf-8')).get('fulltime', {}) if _policy_file else {}


# 全职工作簿布局常量。日期区会因统计区新增字段而发生右移，
# 实际读取时优先按第 6 行表头动态定位，以兼容旧版已发布工作簿。
STAT_START_COL = 3  # C
DEFAULT_DAY_START_COL = 32  # AF（旧版模板）
DEFAULT_REMARK_COL = 63  # BK（31 天旧版模板）
LATE_EARLY_DEDUCTION_LABEL = '迟到/早退扣款'

# ==================== main_judge 常量 ====================
DEFAULT_CONFIG = {
    'legal_holidays': ['0101','0501','0502','0927',
                       '1001','1002','1003','1004','1005','1006','1007'],
    'legal_holidays_by_year': {
        2026: [
            '0101',
            '0216', '0217', '0218', '0219',
            '0405',
            '0501', '0502',
            '0619',
            '0925',
            '1001', '1002', '1003',
        ],
    },
    'no_overtime_list': _POLICY.get('NO_OVERTIME_NAMES', []),
    'overtime_levels': {20:'加班8点',21:'加班9点',22:'加班10点'},
    'night_shift_on': 13,
    'night_shift_off': 23,
}

LEVEL1_HOLIDAYS = ['事假','调休','年假','病假','司龄','婚假','产假','丧假','丧陪']
LEVEL1_ABNORMAL = ['出差','迟到','缺卡','补卡','旷工','外出']
LEAVE_TYPES = ['事假','调休','年假','病假','司龄']

# ==================== calc_stats 常量 ====================
PARTIAL_TYPES = LEAVE_TYPES + ['婚假','丧假','出差']
PAID_ATTENDANCE_TYPES = ['年假','调休','婚假','丧假','司龄','法定']
UNPAID_ATTENDANCE_TYPES = ['事假','病假']
MEAL_DEDUCT_TYPES = ['调休','年假','婚假','丧假','司龄']
# “正常”只反映实际正常上班时段：部分假期按剩余工时计入，部分出差不扣减正常；
# 加班作为独立统计项，不重复计入“正常”。
NORMAL_PARTIAL_TYPES = LEAVE_TYPES + ['婚假','丧假']
HANGZHOU_FULL_ATTENDANCE_PAID_TYPES = ['病假','调休','年假','婚假','丧假','司龄']
COMPANY_LOCATION_KEYWORDS = _POLICY.get('COMPANY_LOCATION_KEYWORDS', [])

RESIGNED_NAMES = set(_POLICY.get('RESIGNED_NAMES', ()))

ATTENDANCE_GROUP_DEFS = {
    'double_fri_sat': {'rest_weekdays': {4, 5}, 'actual_from_schedule': True},
    'single_sunday': {'rest_weekdays': {6}, 'actual_from_schedule': True},
    'alternate_sun_mon': {'rest_weekdays': {0, 6}, 'scheduled_mode': 'alternate_ceil', 'actual_from_schedule': True},
    'alternate_nonfixed': {'scheduled_mode': 'alternate_floor', 'actual_from_schedule': True},
    # 仅按单双休排班计算应出勤；实际出勤仍以当月每日状态为准。
    # 用于月中入职人员，避免把入职前日期误算为实际出勤。
    'alternate_schedule_only': {'scheduled_mode': 'alternate_floor'},
    'month_rest4': {'rest_count': 4, 'actual_from_schedule': True},
    'no_clock_anchor': {'rest_count': None, 'no_clock_required': True},
}

ATTENDANCE_GROUP_BY_NAME = _POLICY.get('ATTENDANCE_GROUP_BY_NAME', {})

FULL_MONTH_INACTIVE_NAMES = set(_POLICY.get('FULL_MONTH_INACTIVE_NAMES', ()))

# H列“计入工资核算天数”的固定口径。
# 原逻辑会读取同目录下其他xlsx的H列公式作为模板，线上临时目录没有该模板，
# 导致 H 列和依赖 H 列的餐补 AD 列为 0。人员例外由环境策略提供。
PAYROLL_DIRECT_NAMES = set(_POLICY.get('PAYROLL_DIRECT_NAMES', ()))
PAYROLL_FIXED_DAYS = _POLICY.get('PAYROLL_FIXED_DAYS', {})
SCHEDULED_DAYS_BY_NAME = _POLICY.get('SCHEDULED_DAYS_BY_NAME', {})

MONTH_REST4_NAMES = set(_POLICY.get('MONTH_REST4_NAMES', ()))
PLAIN_REST_AS_WORKDAY_NAMES = set(_POLICY.get('PLAIN_REST_AS_WORKDAY_NAMES', ()))

SPECIAL_REST_WEEKDAYS_BY_NAME = _POLICY.get('SPECIAL_REST_WEEKDAYS_BY_NAME', {})

# 普通双休计薪使用双休平均月
# 基础 21.75，H 列按 F * 21.75 / 当月周一至周五应出勤天数折算。
STANDARD_DOUBLE_REST_NAMES = set(_POLICY.get('STANDARD_DOUBLE_REST_NAMES', ()))


def _payroll_template_for_name(name):
    if name in PAYROLL_FIXED_DAYS:
        return ('fixed', PAYROLL_FIXED_DAYS[name])
    if name in PAYROLL_DIRECT_NAMES:
        return ('direct_f', None)
    return ('scale_21', None)


def _employee_group_key(name):
    for key, group_key in ATTENDANCE_GROUP_BY_NAME.items():
        if key in name:
            return group_key
    return None


def _count_weekdays(year, month, weekdays):
    days = calendar.monthrange(year, month)[1]
    return sum(1 for d in range(1, days + 1) if datetime.date(year, month, d).weekday() in weekdays)


def _alternate_rest_count(year, month, calendar_days, round_up=False):
    """单双休：每周日休，周六按单双休比例取整。"""
    sunday_count = _count_weekdays(year, month, {6})
    saturday_count = _count_weekdays(year, month, {5})
    saturday_rest_count = ((saturday_count + 1) // 2) if round_up else (saturday_count // 2)
    return sunday_count + saturday_rest_count


def _schedule_info_for_name(name, year, month, calendar_days):
    group_key = _employee_group_key(name)
    if not group_key:
        return None
    info = dict(ATTENDANCE_GROUP_DEFS.get(group_key, {}))
    info['group_key'] = group_key
    rest_weekdays = info.get('rest_weekdays')
    if info.get('scheduled_mode') in ('alternate_floor', 'alternate_ceil'):
        rest_count = _alternate_rest_count(
            year,
            month,
            calendar_days,
            round_up=info.get('scheduled_mode') == 'alternate_ceil',
        )
    elif rest_weekdays is not None:
        rest_count = _count_weekdays(year, month, rest_weekdays)
    else:
        rest_count = info.get('rest_count')
    if rest_count is not None:
        info['rest_count'] = int(rest_count)
        info['scheduled_days'] = max(calendar_days - int(rest_count), 0)
    return info


def _scheduled_days_for_name(name):
    return SCHEDULED_DAYS_BY_NAME.get(name, 21.75)


def _is_month_rest4_name(name):
    return any(key in name for key in MONTH_REST4_NAMES)


def _dynamic_scheduled_days(name, year, month, calendar_days):
    schedule_info = _schedule_info_for_name(name, year, month, calendar_days)
    if schedule_info and schedule_info.get('scheduled_days') is not None:
        return float(schedule_info['scheduled_days'])
    if _is_month_rest4_name(name):
        return max(calendar_days - 4, 0)
    return None


def _double_rest_payroll_denominator(year, month):
    """双休人员 H 列的当月实际应出勤天数（周一至周五）。

    G 列保留人工表的平均月基础天数 21.75；H 列沿用人工公式
    ``F * (G / 当月实际应出勤天数)``。因此分母必须按当前年月计算，
    不能固定为 22。
    """
    days = calendar.monthrange(year, month)[1]
    return float(sum(
        1 for day in range(1, days + 1)
        if datetime.date(year, month, day).weekday() < 5
    ))


def _is_fixed_two_day_rest_schedule(name, year, month, calendar_days):
    """是否为固定每周休两天、但并非周六日休的双休班制。

    这类员工的 G 列保留当月实际应出勤天数（例如周五周六休的
    2026-07 为 22），但 H 列仍须按双休平均月基础 21.75 折算。
    """
    info = _schedule_info_for_name(name, year, month, calendar_days) or {}
    rest_weekdays = info.get('rest_weekdays')
    return bool(
        rest_weekdays is not None
        and len(set(rest_weekdays)) == 2
        and info.get('scheduled_mode') not in ('alternate_floor', 'alternate_ceil')
    )


def _truncate_fraction(value):
    try:
        return int(float(value or 0))
    except Exception:
        return 0


def _travel_stat_units_from_hours(hours):
    """考勤统计口径：出差按实际小时折算为天数。"""
    try:
        hours = float(hours or 0)
    except Exception:
        return 0.0
    return min(max(hours, 0), 8) / 8


def _travel_subsidy_units_from_hours(hours):
    """补贴口径：大于 4 小时才计半天，之后每满 4 小时递增半天。"""
    try:
        hours = float(hours or 0)
    except Exception:
        return 0.0
    if hours <= 4:
        return 0.0
    if hours <= 8:
        return 0.5
    return (int((hours - 0.0001) // 4) + 1) * 0.5


TRAVEL_HIGH_RATE_CITIES = ('北京', '上海', '广州', '深圳')


def _high_rate_travel_city(value):
    """Return the first 北上广深 city found in a travel text/address."""
    text = str(value or '')
    for city in TRAVEL_HIGH_RATE_CITIES:
        if city in text:
            return city
    return ''


def _raw_source_day(value):
    """Extract the day number from DingTalk's raw attendance date text."""
    if hasattr(value, 'day'):
        return int(value.day)
    text = str(value or '')
    match = re.search(r'(?:\d{2}|\d{4})[-/](\d{1,2})[-/](\d{1,2})', text)
    return int(match.group(2)) if match else None


def _name_variants(value):
    """Return exact and real-name variants used by different source sheets."""
    text = str(value or '').strip()
    if not text:
        return []
    variants = [text]
    if '-' in text:
        real_name = text.split('-', 1)[-1].strip()
        if real_name and real_name not in variants:
            variants.append(real_name)
    return variants


def _build_raw_trip_city_map(ws_src):
    """Map (employee-name variant, day) to high-rate cities in raw records.

    The monthly summary keeps the travel application and duration but not the
    raw punch address.  The raw sheet is the authoritative place for the
    address, so the subsidy calculation can use it when the monthly text has
    no city name.
    """
    result = {}
    if ws_src is None:
        return result
    for row in range(4, ws_src.max_row + 1):
        raw_name = str(ws_src.cell(row, 1).value or '').strip()
        if not raw_name:
            continue
        day = _raw_source_day(ws_src.cell(row, 7).value or ws_src.cell(row, 8).value)
        if not day:
            continue
        trip_text = ' '.join(str(ws_src.cell(row, col).value or '') for col in (10, 12, 13))
        if '出差' not in trip_text:
            continue
        city = _high_rate_travel_city(ws_src.cell(row, 11).value)
        if not city:
            continue
        for name in _name_variants(raw_name):
            result.setdefault((name, day), set()).add(city)
    return result


def _raw_trip_cities(city_map, raw_name, days):
    cities = set()
    for name in _name_variants(raw_name):
        for day in days or ():
            cities.update(city_map.get((name, day), set()))
    return cities


def _legal_holidays_for_year(config, year):
    by_year = config.get('legal_holidays_by_year') or {}
    values = by_year.get(year) or by_year.get(str(year))
    if values:
        return {str(v).zfill(4) for v in values}
    return {str(v).zfill(4) for v in config.get('legal_holidays', [])}


def _last_employee_row(ws, start=8, name_col=3):
    last = start - 1
    for r in range(start, ws.max_row + 1):
        if str(ws.cell(r, name_col).value or '').strip():
            last = r
    return last


def _attendance_day_columns(ws, days_count=None, start_col=None):
    cols = {}
    max_day = days_count or 31
    if start_col is None:
        start_col = DEFAULT_DAY_START_COL
        for col in range(STAT_START_COL, ws.max_column + 1):
            value = ws.cell(6, col).value
            try:
                day = int(float(value))
            except Exception:
                continue
            if 1 <= day <= max_day:
                start_col = col
                break
    for col in range(start_col, ws.max_column + 1):
        value = ws.cell(6, col).value
        try:
            day = int(float(value))
        except Exception:
            continue
        if 1 <= day <= max_day:
            cols[day] = col
    return cols


def _remark_col(ws, day_cols=None):
    start_col = min(day_cols.values()) if day_cols else DEFAULT_DAY_START_COL
    for row in (5, 6, 7):
        for col in range(start_col, ws.max_column + 2):
            if str(ws.cell(row, col).value or '').strip() == '备注':
                return col
    if day_cols:
        return max(day_cols.values()) + 1
    return DEFAULT_REMARK_COL


ATTENDANCE_STATUS_COLOR_ORDER = [
    '加班10点', '加班9点', '加班8点',
    '司龄', '缺卡', '补卡', '事假', '病假', '年假', '调休', '婚假',
    '出差', '外出', '迟到', '丧假', '丧陪', '旷工', '产假',
]


def _attendance_status_color_key(status, color_map):
    """小时型状态（如事假4.5h）与备注型状态（如离职旷工）按基础状态取色。"""
    text = str(status or '').strip()
    for label in ATTENDANCE_STATUS_COLOR_ORDER:
        lookup = '丧假' if label == '丧陪' else label
        if label in text and lookup in color_map:
            return lookup
    return ''


def _apply_attendance_status_colors(ws, first_row=8, last_row=None):
    """以全职表第2行图例为唯一色值源，同时清理模板旧月份遗留色。"""
    color_map = {}
    for col in range(1, ws.max_column + 1):
        cell = ws.cell(2, col)
        fill = cell.fill
        status = str(cell.value or '').strip()
        if (
            status and fill and fill.fill_type == 'solid'
            and fill.start_color and fill.start_color.type == 'rgb'
            and fill.start_color.rgb not in (None, '00000000')
        ):
            color_map[status] = fill.start_color.rgb

    day_cols = _attendance_day_columns(ws)
    end_row = last_row or _last_employee_row(ws)
    for row in range(first_row, end_row + 1):
        for col in day_cols.values():
            cell = ws.cell(row, col)
            key = _attendance_status_color_key(cell.value, color_map)
            # 先清除模板上一个月留下的颜色；正常、休、法定保持白色。
            cell.fill = PatternFill('solid', fgColor=color_map.get(key, 'FFFFFF'))
    return color_map

def _load_manual_h_templates(file_path):
    """读取同目录人工表的H列模板；找不到时返回空映射。"""
    current = Path(file_path).resolve()
    candidates = [
        p for p in current.parent.glob('*.xlsx')
        if p.resolve() != current
        and '-' not in p.name
        and '_bak' not in p.name
        and not p.name.startswith('~$')
    ]
    if not candidates:
        return {}
    try:
        wb = openpyxl.load_workbook(candidates[0], data_only=False, read_only=True)
        ws = wb.worksheets[0]
    except Exception:
        return {}
    templates = {}
    for r in range(8, _last_employee_row(ws) + 1):
        name = str(ws.cell(r, 3).value or '').strip()
        if not name:
            continue
        h = ws.cell(r, 8).value
        if isinstance(h, str) and h.startswith('='):
            formula = h.replace(' ', '').upper()
            if 'G' in formula and '/21' in formula:
                templates[name] = ('scale_21', None)
            elif formula.startswith('=F'):
                templates[name] = ('direct_f', None)
        elif h not in (None, ''):
            try:
                templates[name] = ('fixed', float(h))
            except Exception:
                pass
    return templates

def _time_to_minutes(value):
    h, m = map(int, value.split(':'))
    return h * 60 + m

def _outside_covers_punch_window(text):
    """外出覆盖上班8:30-9:00或下班17:30后时，不按缺卡处理。"""
    morning_start, morning_end = 8 * 60 + 30, 9 * 60
    evening_start, evening_end = 17 * 60 + 30, 24 * 60
    for m in re.finditer(r'外出\d{2}-\d{2} (\d{1,2}:\d{2})到\d{2}-\d{2} (\d{1,2}:\d{2})', text):
        start = _time_to_minutes(m.group(1))
        end = _time_to_minutes(m.group(2))
        if end < start:
            end += 24 * 60
        covers_morning = start <= morning_end and end >= morning_start
        covers_evening = start <= evening_end and end >= evening_start
        if covers_morning or covers_evening:
            return True
    return False

# ==================== check_wifi 常量 ====================
WIFI_MACS = _POLICY.get('WIFI_MACS', [])

# ==================== 考勤区判定 ====================
def _main_judge(file_path, config=None):
    if config is None: config = DEFAULT_CONFIG
    else: c = DEFAULT_CONFIG.copy(); c.update(config); config = c

    wb = openpyxl.load_workbook(file_path)
    wb_v = openpyxl.load_workbook(file_path, data_only=True)
    ws_data = wb_v['月度数据']; ws_full = wb_v['全职']; ws_w = wb['全职']
    raw_trip_city_map = _build_raw_trip_city_map(wb_v['原始数据'])

    # 小函数
    def parse_entry_date(value):
        if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
            return f'{int(value.year):04d}-{int(value.month):02d}-{int(value.day):02d}'
        s = str(value or '').strip()
        m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
        if not m:
            return ''
        return f'{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'

    def is_legal_day_for_employee(day, emp_config):
        if emp_config.get('no_clock_required') or emp_config.get('full_month_inactive'):
            return False
        if emp_config.get('is_separated') and day > int(emp_config.get('last_active_day') or 0):
            return False
        mmdd = f'{month:02d}{day:02d}'
        entry_date = emp_config.get('entry_date')
        if not entry_date:
            return False
        legal_holidays = _legal_holidays_for_year(config, year)
        return mmdd in legal_holidays and entry_date < f"{year}-{mmdd[:2]}-{mmdd[2:]}"

    def is_double_rest_employee(emp_config):
        try:
            scheduled = float(emp_config.get('scheduled_days') or 0)
        except Exception:
            scheduled = 0.0
        return abs(scheduled - 21.75) < 0.001

    def special_rest_weekdays(emp_config):
        schedule_info = emp_config.get('schedule_info') or {}
        if schedule_info.get('rest_weekdays') is not None:
            return schedule_info.get('rest_weekdays')
        name = emp_config.get('matched_name') or ''
        for key, weekdays in SPECIAL_REST_WEEKDAYS_BY_NAME.items():
            if key in name:
                return weekdays
        return None

    def is_special_workday_rest_punch(day, text, emp_config):
        weekdays = special_rest_weekdays(emp_config)
        if weekdays is None:
            return False
        if datetime.date(year, month, day).weekday() in weekdays:
            return False
        blocking_keywords = LEVEL1_HOLIDAYS + ['出差', '外出', '迟到', '缺卡', '旷工', '加班']
        return not any(k in text for k in blocking_keywords)

    def is_plain_rest_as_workday(day, text, emp_config):
        name = emp_config.get('matched_name') or ''
        if not any(key in name for key in PLAIN_REST_AS_WORKDAY_NAMES):
            return False
        return is_special_workday_rest_punch(day, text, emp_config)

    def is_after_separation_active_range(day, emp_config):
        return emp_config.get('is_separated') and day > int(emp_config.get('last_active_day') or 0)

    def is_rest_candidate_day(day, raw_value, emp_config):
        if is_legal_day_for_employee(day, emp_config):
            return False
        if raw_value is None or str(raw_value).strip() == '':
            return True
        return '休息' in str(raw_value)

    def resolve_alternate_rest_days(emp_config):
        """Resolve the monthly rest quota for the Sun/Mon alternating schedule.

        DingTalk may mark a scheduled rest day as "旷工". For this one
        rotating schedule, explicit rest days are kept first; then a pure
        absence on Saturday/Sunday/Monday is used only when it is necessary
        to reach that month's configured alternating-rest quota.
        """
        schedule_info = emp_config.get('schedule_info') or {}
        if schedule_info.get('group_key') != 'alternate_sun_mon':
            return set()

        rest_quota = int(schedule_info.get('rest_count') or 0)
        if rest_quota <= 0:
            return set()

        raw_by_day = emp_config.get('raw_by_day') or {}
        fixed_rest_weekdays = set(schedule_info.get('rest_weekdays') or set())
        fallback_weekdays = fixed_rest_weekdays | {5}  # Sat/Sun/Mon
        blocking_keywords = LEVEL1_HOLIDAYS + ['出差', '外出', '迟到', '缺卡', '补卡', '加班']

        def is_pure_absence(text):
            return '旷工' in text and not any(keyword in text for keyword in blocking_keywords)

        resolved = []
        for d in range(1, days_count + 1):
            if is_legal_day_for_employee(d, emp_config):
                continue
            text = str(raw_by_day.get(d) or '')
            weekday = datetime.date(year, month, d).weekday()
            if '休息' in text and '休息并打卡' not in text:
                resolved.append(d)
            elif is_pure_absence(text) and weekday in fixed_rest_weekdays:
                resolved.append(d)

        if len(resolved) < rest_quota:
            for d in range(1, days_count + 1):
                if d in resolved or is_legal_day_for_employee(d, emp_config):
                    continue
                text = str(raw_by_day.get(d) or '')
                weekday = datetime.date(year, month, d).weekday()
                if is_pure_absence(text) and weekday in fallback_weekdays:
                    resolved.append(d)
                    if len(resolved) >= rest_quota:
                        break

        return set(resolved[:rest_quota])

    def active_days_for_long_leave(start_day, end_day, emp_config):
        covered = [
            d for d in range(start_day, end_day + 1)
            if 1 <= d <= days_count
        ]
        if not covered:
            return []

        schedule_info = emp_config.get('schedule_info') or {}
        resolved_rest_days = emp_config.get('resolved_rest_days') or set()
        if resolved_rest_days:
            return [d for d in covered if d not in resolved_rest_days]
        rest_weekdays = schedule_info.get('rest_weekdays')
        if rest_weekdays is not None:
            return [
                d for d in covered
                if datetime.date(year, month, d).weekday() not in rest_weekdays
            ]

        if is_double_rest_employee(emp_config):
            return [d for d in covered if datetime.date(year, month, d).weekday() < 5]

        try:
            scheduled = float(emp_config.get('scheduled_days') or 0)
        except Exception:
            scheduled = 0.0
        if not scheduled:
            return covered

        rest_quota = max(days_count - int(round(scheduled)), 0)
        raw_by_day = emp_config.get('raw_by_day') or {}
        rest_outside = sum(
            1 for d in range(1, days_count + 1)
            if d not in covered and is_rest_candidate_day(d, raw_by_day.get(d), emp_config)
        )
        rests_to_keep = max(rest_quota - rest_outside, 0)
        if rests_to_keep <= 0:
            return covered

        rest_candidates = [
            d for d in covered
            if is_rest_candidate_day(d, raw_by_day.get(d), emp_config)
        ]
        weekend_candidates = [d for d in covered if datetime.date(year, month, d).weekday() >= 5 and d not in rest_candidates]
        exclude = (rest_candidates + weekend_candidates)[:rests_to_keep]
        return [d for d in covered if d not in set(exclude)]

    def layer1_keywords(raw_text, day, month, emp_config):
        states = []
        if not raw_text: return states
        text = str(raw_text).replace('\n',' | ')
        mmdd = f'{month:02d}{day:02d}'
        entry_mmdd = emp_config.get('entry_mmdd','')
        entry_date = emp_config.get('entry_date','')

        if day in (emp_config.get('resolved_rest_days') or set()):
            if '旷工' in text and not any(k in text for k in LEVEL1_HOLIDAYS + ['出差', '外出', '迟到', '缺卡', '补卡', '加班']):
                return ['休']

        rest_weekdays = special_rest_weekdays(emp_config)
        if rest_weekdays is not None and datetime.date(year, month, day).weekday() in rest_weekdays:
            if '旷工' in text and not any(k in text for k in ['出差', '外出', '事假', '病假', '年假', '调休', '司龄', '婚假', '产假', '丧假']):
                return ['休']

        for h in LEVEL1_HOLIDAYS:
            if h not in text: continue
            all_hours = re.findall(rf'{h}.*?(\d+(?:\.\d+)?)(?:小时|h)', text)
            if all_hours:
                hrs = sum(float(x) for x in all_hours)
                dm = re.search(rf'{h}\d{{2}}-(\d{{2}}) \d{{2}}:\d{{2}}\u5230\d{{2}}-(\d{{2}})', text)
                if dm and dm.group(1) != dm.group(2):
                    start_day = int(dm.group(1))
                    end_day = int(dm.group(2))
                    days = end_day - start_day + 1
                    if days > 2:
                        active_days = active_days_for_long_leave(start_day, end_day, emp_config)
                        if day not in active_days:
                            continue
                        hrs = hrs / max(len(active_days), 1)
                    else:
                        hrs = hrs / days
                state = f'{h}{int(hrs)}.0h' if hrs == int(hrs) else f'{h}{hrs}h'
            else:
                state = h
            states.append(state)

        for a in LEVEL1_ABNORMAL:
            if a not in text: continue
            if a == '出差':
                all_hours = re.findall(r'出差.*?(\d+(?:\.\d+)?)(?:小时|h)', text)
                if all_hours:
                    hrs = sum(float(x) for x in all_hours)
                    dm = re.search(r'出差\d{2}-(\d{2}) \d{2}:\d{2}到\d{2}-(\d{2})', text)
                    if dm and dm.group(1) != dm.group(2):
                        start_day = int(dm.group(1))
                        end_day = int(dm.group(2))
                        days = end_day - start_day + 1
                        if days > 2:
                            workdays = sum(
                                1 for d in range(start_day, end_day + 1)
                                if datetime.date(year, month, d).weekday() < 5
                            )
                            hrs = hrs / max(workdays, 1)
                        else:
                            hrs = hrs / days
                    if hrs >= 8:
                        states.append('出差')
                    else:
                        states.append(f'出差{int(hrs)}.0h' if hrs == int(hrs) else f'出差{hrs}h')
                else:
                    states.append('出差')
                continue
            if a == '迟到':
                if mmdd == entry_mmdd: continue
            if a == '缺卡':
                if mmdd == entry_mmdd: continue
                if _outside_covers_punch_window(text): continue
            if a == '外出':
                dm = re.search(r'外出\d{2}-(\d{2}) \d{2}:\d{2}到\d{2}-(\d{2})', text)
                if dm and dm.group(1) != dm.group(2):
                    start_day = int(dm.group(1))
                    end_day = int(dm.group(2))
                    if end_day - start_day + 1 > 2:
                        if datetime.date(year, month, day).weekday() >= 5:
                            continue
                states.append('外出'); continue
            states.append(a)

        # 非法定补班/休息日出勤仍按休/法定；休息出差的补贴依据在下方保留到备注。
        if '休息并打卡' in text:
            if is_legal_day_for_employee(day, emp_config):
                states.append('法定')
            elif is_special_workday_rest_punch(day, text, emp_config):
                states.append('正常')
            else:
                states.append('休')
        elif '休息' in text:
            if is_legal_day_for_employee(day, emp_config):
                states.append('法定')
            elif is_plain_rest_as_workday(day, text, emp_config):
                states.append('正常')
            else:
                states.append('休')

        # 休息日: 主状态写休/法定；休息出差保留到备注用于出差补贴，外出不参与出勤。
        if any(s in ('休','法定') for s in states):
            rest_state = '法定' if '法定' in states else '休'
            trip_states = [s for s in states if s == '出差' or re.match(r'出差\d+(?:\.\d+)?h', s)]
            states = [rest_state] + trip_states

        return states

    def layer2_overtime(raw_text, emp_config):
        if emp_config.get('no_overtime',False): return None
        if not raw_text: return None
        if '外出' in str(raw_text): return None
        raw = str(raw_text)
        if '加班' in raw:
            # 已走加班审批的记录按正常考勤处理，不能再按下班时间重复生成加班状态/补贴。
            # 月度汇总会把跨天审批文本复制到多个日期；若在这里按时间识别，会造成重复计补贴。
            return None
        m = re.search(r'\((?:-|\d{1,2}:\d{2}),(\d{1,2}:\d{2})\)', raw)
        if not m: return None
        off_h = int(m.group(1).split(':')[0])
        on_h = 99
        m2 = re.search(r'\((\d{1,2}:\d{2}),', raw)
        if m2: on_h = int(m2.group(1).split(':')[0])
        if on_h >= config['night_shift_on'] and off_h >= config['night_shift_off']:
            return None
        for th in sorted(config['overtime_levels'].keys(), reverse=True):
            if off_h >= th: return config['overtime_levels'][th]
        return None

    def layer3_fallback(raw_text, day, emp_config):
        if not raw_text: return '法定' if is_legal_day_for_employee(day, emp_config) else '休'
        s = str(raw_text)
        if re.search(r'\d{1,2}:\d{2},\d{1,2}:\d{2}', s): return '正常'
        if re.search(r'\(\d{1,2}:\d{2}\)', s) or re.search(r'\(-,\d{1,2}:\d{2}\)', s) or re.search(r'\(\d{1,2}:\d{2},-', s):
            return '缺卡'
        return '法定' if is_legal_day_for_employee(day, emp_config) else '休'

    # 年月
    year_cell = ws_full.cell(3,3).value
    try: year = int(year_cell)
    except: year = datetime.date.today().year

    month_cell = ws_full.cell(3,4).value
    try: month = int(month_cell)
    except: month = 5
    days_count = 31 if month in [1,3,5,7,8,10,12] else 28 if month == 2 else 30

    # 员工映射（从公式模式读）
    full_map = {}
    last_full_row = _last_employee_row(ws_w)
    for r in range(8, last_full_row + 1):
        n = str(ws_w.cell(r,3).value or '').strip()
        if n: full_map[n] = r

    day_cols = _attendance_day_columns(ws_w, days_count)
    remark_col = _remark_col(ws_w, day_cols)

    def is_meaningful_active_text(value):
        text = str(value or '').strip()
        if not text:
            return False
        if '休息' in text and not re.search(r'\d{1,2}:\d{2}', text):
            return False
        if '未加入考勤组' in text and not re.search(r'\d{1,2}:\d{2}', text):
            return False
        active_keys = ['正常', '迟到', '缺卡', '补卡', '外出', '出差', '旷工', '事假', '病假', '年假', '调休', '司龄', '婚假', '产假', '丧假']
        return bool(re.search(r'\d{1,2}:\d{2}', text) or any(k in text for k in active_keys))

    # 清空考勤区+备注
    for r in range(8, last_full_row + 1):
        for d in range(1, days_count+1):
            col = day_cols.get(d)
            if col:
                ws_w.cell(r, col).value = None
        ws_w.cell(r, remark_col).value = None
        if remark_col != 63:
            ws_w.cell(r, 63).value = None

    stats = {}; total = 0

    for mr in range(5, ws_data.max_row+1):
        raw_name = str(ws_data.cell(mr,1).value or '').strip()
        if not raw_name: continue

        matched_row = None; matched_name = None
        for fn, fr in full_map.items():
            if fn in raw_name:
                matched_row = fr; matched_name = fn; break

        if not matched_row: continue

        emp_config = {}
        emp_config['matched_name'] = matched_name
        emp_config['raw_name'] = raw_name
        emp_config['is_separated'] = '离职' in raw_name or any(n in matched_name for n in RESIGNED_NAMES)
        emp_config['full_month_inactive'] = any(n in matched_name for n in FULL_MONTH_INACTIVE_NAMES)
        schedule_info = _schedule_info_for_name(matched_name, year, month, days_count) or {}
        emp_config['schedule_info'] = schedule_info
        emp_config['no_clock_required'] = bool(schedule_info.get('no_clock_required'))
        ed = parse_entry_date(ws_full.cell(matched_row,4).value)
        if ed:
            emp_config['entry_date'] = ed
            emp_config['entry_mmdd'] = ed[5:7]+ed[8:10]
        emp_config['no_overtime'] = any(nb in matched_name for nb in config['no_overtime_list'])
        try:
            emp_config['scheduled_days'] = float(ws_full.cell(matched_row, 7).value or 0)
        except Exception:
            emp_config['scheduled_days'] = 0.0
        if not emp_config['scheduled_days']:
            fallback_scheduled = _scheduled_days_for_name(matched_name)
            emp_config['scheduled_days'] = float(fallback_scheduled or 0)
        emp_config['attendance_group'] = ws_data.cell(mr, 2).value
        emp_config['department'] = ws_data.cell(mr, 3).value
        emp_config['position'] = ws_data.cell(mr, 5).value
        emp_config['raw_by_day'] = {
            d: ws_data.cell(mr, 43 + d).value
            for d in range(1, days_count + 1)
        }
        emp_config['last_active_day'] = max(
            [d for d, v in emp_config['raw_by_day'].items() if is_meaningful_active_text(v)]
            or [0]
        )
        emp_config['resolved_rest_days'] = resolve_alternate_rest_days(emp_config)

        day_remarks = []
        for day in range(1, days_count+1):
            raw_val = ws_data.cell(mr, 43+day).value
            if emp_config.get('no_clock_required'):
                state = '休'
                if day in day_cols:
                    ws_w.cell(matched_row, day_cols[day], state)
                stats[state] = stats.get(state,0)+1; total += 1; continue
            if is_after_separation_active_range(day, emp_config):
                state = '休'
                if day in day_cols:
                    ws_w.cell(matched_row, day_cols[day], state)
                stats[state] = stats.get(state,0)+1; total += 1; continue
            if raw_val is None or str(raw_val).strip() == '':
                state = '法定' if is_legal_day_for_employee(day, emp_config) else '休'
                if day in day_cols:
                    ws_w.cell(matched_row, day_cols[day], state)
                stats[state] = stats.get(state,0)+1; total += 1; continue

            raw_text = str(raw_val)
            states = layer1_keywords(raw_text, day, month, emp_config)

            # 已判休/法定不继续判加班
            if not any(s in ('休', '法定') for s in states):
                ot = layer2_overtime(raw_text, emp_config)
                if ot: states.append(ot)

            # 补卡/缺卡+加班: 加班优先做主状态
            if len(states) > 1 and any('加班' in s for s in states) and any(s in ('补卡','缺卡') for s in states):
                states.sort(key=lambda s: (0 if '加班' in s else (1 if s in ('补卡','缺卡') else 2)))

            if not states:
                state = layer3_fallback(raw_text, day, emp_config)
                mmdd = f'{month:02d}{day:02d}'
                if state == '缺卡' and mmdd == emp_config.get('entry_mmdd',''):
                    state = '正常'
                if day in day_cols:
                    ws_w.cell(matched_row, day_cols[day], state)
                stats[state] = stats.get(state,0)+1; total += 1; continue

            main_state = states[0]
            # 旷工+离职→特殊备注
            if main_state == '旷工' and '离职' in raw_name:
                main_state = '旷工（该同事已经离职，请检查交接是否结束，是否需要判定为休)'
            if day in day_cols:
                ws_w.cell(matched_row, day_cols[day], main_state)
            stats[main_state] = stats.get(main_state,0)+1; total += 1
            for ss in states[1:]:
                day_remarks.append(f'{month:02d}月{day}.0日{ss}')

            # 出差北上广深: BK标记100元。城市可能只存在原始打卡地址，
            # 不能只检查月度汇总文本。
            if main_state == '出差':
                has_high_rate_city = bool(_high_rate_travel_city(raw_text))
                if not has_high_rate_city:
                    has_high_rate_city = bool(
                        _raw_trip_cities(raw_trip_city_map, raw_name, {day})
                    )
                if has_high_rate_city:
                    day_remarks.append(f'{month:02d}月{day}.0日出差北上广深')

        if day_remarks:
            ws_w.cell(matched_row, remark_col, '  '.join(day_remarks))

    _apply_attendance_status_colors(ws_w, 8, last_full_row)

    for attempt in range(5):
        try: wb.save(file_path); break
        except: time.sleep(2)
    return {'total': total, 'stats': stats}


# ==================== 迟到/早退扣款 ====================
LATE_EARLY_FREE_COUNT = 3
LATE_EARLY_GRACE_MINUTES = 5
LATE_EARLY_MID_LIMIT_MINUTES = 30
LATE_EARLY_SEVERE_LIMIT_MINUTES = 60
LATE_EARLY_ABSENCE_LIMIT_MINUTES = 240


def _parse_late_early_events(text, day=None):
    """从月度数据的每日原文提取迟到/早退事件及分钟数。"""
    value = str(text or '')
    pattern = re.compile(
        r'(?P<kind>(?:上班)?迟到|(?:下班)?早退)\s*'
        r'(?P<minutes>\d+(?:\.\d+)?)\s*(?:分钟|分)'
    )
    events = []
    for match in pattern.finditer(value):
        event = {
            'kind': match.group('kind'),
            'minutes': float(match.group('minutes'))
        }
        if day is not None:
            event['day'] = int(day)
        events.append(event)
    return events


def _calc_late_early_deduction(events, entry_day=None):
    """按已确认制度计算月度迟到/早退金额扣款。

    每月 3 次机会仅由不超过 5 分钟的迟到/早退事件消耗；这 3 次免扣。
    超过 5 分钟的事件直接进入 20/50 元档位，不占用上述免扣机会。达到
    1 小时或 4 小时的事件进入旷工折算分支，不写入金额列，也不占用免扣
    机会；本次先不改变现有出勤/计薪字段，避免把陈先彬这类原始记录与
    月度汇总不一致的问题扩展到工资结果。
    入职当天的上班迟到不计入扣款，也不消耗当月机会；当天的下班早退
    仍按制度计算。
    """
    amount = 0
    free_count = 0
    for event in events or []:
        kind = str(event.get('kind') or '').strip()
        try:
            event_day = int(event.get('day'))
        except (TypeError, ValueError):
            event_day = None
        if (
            entry_day is not None
            and event_day == int(entry_day)
            and kind in ('迟到', '上班迟到')
        ):
            continue
        try:
            minutes = max(float(event.get('minutes') or 0), 0)
        except (TypeError, ValueError):
            continue
        # 1 小时以上按旷工折算，属于另一条出勤结果规则；在该规则尚未
        # 接入 H/F 的阶段，不应反过来消耗金额扣款的 3 次机会。
        if minutes >= LATE_EARLY_SEVERE_LIMIT_MINUTES:
            continue
        if (
            minutes <= LATE_EARLY_GRACE_MINUTES
        ):
            if free_count < LATE_EARLY_FREE_COUNT:
                free_count += 1
                continue
            amount += 20
            continue
        if minutes <= LATE_EARLY_MID_LIMIT_MINUTES:
            amount += 20
        elif minutes < LATE_EARLY_SEVERE_LIMIT_MINUTES:
            amount += 50
    return amount


# ==================== 统计区计算 ====================
def _calc_statistics(file_path, ningbo_list=None):
    if ningbo_list is None:
        ningbo_list = []

    # 人工表里有餐补AD公式的宁波员工（硬编码）
    NB_HAS_AD = set(_POLICY.get('NINGBO_FULL_ATTENDANCE_NAMES', ()))

    wb_v = openpyxl.load_workbook(file_path, data_only=True)
    wb_f = openpyxl.load_workbook(file_path)
    ws_d = wb_v['全职']
    ws_m = wb_v['月度数据']
    raw_trip_city_map = _build_raw_trip_city_map(wb_v['原始数据'])
    ws_w = wb_f['全职']
    # H列必须由脚本内规则决定，不能依赖运行目录下是否存在历史人工表。
    h_templates = {}
    day_cols = _attendance_day_columns(ws_d)
    remark_col = _remark_col(ws_d, day_cols)
    calendar_days = len(day_cols) or 31
    stat_header_cols = {
        str(ws_w.cell(6, col).value or '').strip(): col
        for col in range(STAT_START_COL, min(day_cols.values() or [ws_w.max_column + 1]))
        if str(ws_w.cell(6, col).value or '').strip()
    }
    deduction_col = stat_header_cols.get(LATE_EARLY_DEDUCTION_LABEL)
    overtime_subsidy_col = stat_header_cols.get('加班补贴', 28)
    travel_subsidy_col = stat_header_cols.get('出差补贴', 29)
    meal_allowance_col = stat_header_cols.get('餐补', 30)
    full_attendance_col = stat_header_cols.get('全勤', 31)

    try:
        stat_year = int(ws_w.cell(3, 3).value or ws_d.cell(3, 3).value)
    except Exception:
        stat_year = datetime.date.today().year
    try:
        stat_month = int(ws_w.cell(3, 4).value or ws_d.cell(3, 4).value)
    except Exception:
        stat_month = 5

    def parse_travel_applications(text, raw_name='', day=None):
        result = []
        s = str(text or '').replace('\n', ' | ')
        for m in re.finditer(r'出差[^,，|]*', s):
            token = m.group(0).strip()
            if not token:
                continue
            units = 1.0
            dm = re.search(r'(\d+(?:\.\d+)?)\s*天', token)
            hm = re.search(r'(\d+(?:\.\d+)?)\s*(?:小时|h)', token)
            if dm:
                units = float(dm.group(1))
            elif hm:
                hours = float(hm.group(1))
                units = _travel_subsidy_units_from_hours(hours)
            if units <= 0:
                continue
            days = set()
            span = re.search(r'出差\d{2}-(\d{1,2}) .*?到\d{2}-(\d{1,2})', token)
            if span:
                start_day, end_day = int(span.group(1)), int(span.group(2))
                days = {d for d in range(start_day, end_day + 1) if 1 <= d <= calendar_days}
            city_days = days or ({day} if day else set())
            has_high_rate_city = bool(_high_rate_travel_city(token))
            if not has_high_rate_city:
                has_high_rate_city = bool(
                    _raw_trip_cities(raw_trip_city_map, raw_name, city_days)
                )
            rate = 100 if has_high_rate_city else 50
            result.append({
                'token': token,
                'units': units,
                'rate': rate,
                'subsidy': units * rate,
                'days': days,
            })
        return result

    monthly_meta = []
    for mr in range(5, ws_m.max_row + 1):
        raw_name = str(ws_m.cell(mr, 1).value or '').strip()
        if not raw_name:
            continue
        travel_by_token = {}
        late_early_events = []
        for d in range(1, calendar_days + 1):
            daily_value = ws_m.cell(mr, 43 + d).value
            for app in parse_travel_applications(daily_value, raw_name, d):
                existing = travel_by_token.get(app['token'])
                if existing is None or app['rate'] > existing.get('rate', 50):
                    travel_by_token[app['token']] = app
            late_early_events.extend(_parse_late_early_events(daily_value, day=d))
        travel_days = set()
        for app in travel_by_token.values():
            travel_days.update(app.get('days') or set())
        monthly_meta.append({
            'raw_name': raw_name,
            'group': str(ws_m.cell(mr, 2).value or '').strip(),
            'department': str(ws_m.cell(mr, 3).value or '').strip(),
            'position': str(ws_m.cell(mr, 5).value or '').strip(),
            'is_separated': '离职' in raw_name or any(n in raw_name for n in RESIGNED_NAMES),
            'travel_units': sum(app['units'] for app in travel_by_token.values()),
            'travel_subsidy': sum(app['subsidy'] for app in travel_by_token.values()),
            'travel_days': travel_days,
            'late_early_events': late_early_events,
        })

    def get_employee_meta(name):
        for item in monthly_meta:
            if name in item['raw_name']:
                return item
        return {'raw_name': '', 'group': '', 'department': '', 'position': '', 'is_separated': False,
                'travel_units': 0.0, 'travel_subsidy': 0.0, 'travel_days': set(),
                'late_early_events': []}

    def entry_in_stat_month(value):
        if hasattr(value, 'year') and hasattr(value, 'month'):
            return value.year == stat_year and value.month == stat_month
        s = str(value or '')
        m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
        return bool(m and int(m.group(1)) == stat_year and int(m.group(2)) == stat_month)

    def entry_after_stat_month_start(value):
        """月初之后入职的员工，当月不享受全勤奖。"""
        if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
            return value.year == stat_year and value.month == stat_month and value.day > 1
        s = str(value or '')
        m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
        return bool(
            m
            and int(m.group(1)) == stat_year
            and int(m.group(2)) == stat_month
            and int(m.group(3)) > 1
        )

    def parse_stat_entry_date(value):
        if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
            return datetime.date(int(value.year), int(value.month), int(value.day))
        s = str(value or '')
        m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', s)
        if not m:
            return None
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def legal_count_for_stats(name, entry_value):
        if name in FULL_MONTH_INACTIVE_NAMES:
            return 0
        entry_date = parse_stat_entry_date(entry_value)
        legal_holidays = _legal_holidays_for_year(DEFAULT_CONFIG, stat_year)
        total = 0
        for d in range(1, calendar_days + 1):
            mmdd = f'{stat_month:02d}{d:02d}'
            if mmdd not in legal_holidays:
                continue
            legal_date = datetime.date(stat_year, stat_month, d)
            if entry_date and entry_date >= legal_date:
                continue
            total += 1
        return total

    COL_MAP = {
        9:'正常', 10:'缺卡', 11:'补卡', 12:'事假', 13:'病假',
        14:'年假', 15:'司龄', 16:'调休', 17:'婚假', 18:'产假',
        19:'丧假', 20:'出差', 21:'外出', 22:'加班8点', 23:'加班9点',
        24:'加班10点', 25:'休', 26:'法定', 27:'迟到',
    }

    def normalize_status(status):
        s = str(status or '').strip()
        if not s:
            return ''
        if '旷工' in s:
            return '旷工'
        if s == '休息':
            return '休'
        if s == '丧陪':
            return '丧假'
        return s

    def split_status(status):
        s = normalize_status(status)
        if not s:
            return '', None
        m = re.match(r'([\u4e00-\u9fa5]+)(\d+(?:\.\d+)?)(?:小时|h)$', s)
        if m:
            lt = '丧假' if m.group(1) == '丧陪' else m.group(1)
            return lt, float(m.group(2))
        return s, None

    def status_travel_units(status):
        base, hrs = split_status(status)
        if base != '出差':
            return 0.0
        if hrs is None:
            return 1.0
        return _travel_stat_units_from_hours(hrs)

    def status_travel_subsidy_units(status):
        base, hrs = split_status(status)
        if base != '出差':
            return 0.0
        if hrs is None:
            return 1.0
        return _travel_subsidy_units_from_hours(hrs)

    def add_stat(status, exact_cnt, partial_extra, normal_extra_box):
        s = normalize_status(status)
        if not s:
            return
        if s == '出差北上广深':
            exact_cnt[s] = exact_cnt.get(s, 0) + 1
            return
        base, hrs = split_status(s)
        if base == '旷工':
            base = '休'
        if hrs is not None and base in PARTIAL_TYPES:
            if base == '出差':
                partial_extra[base] = partial_extra.get(base, 0) + _travel_stat_units_from_hours(hrs)
            else:
                partial_extra[base] = partial_extra.get(base, 0) + min(hrs, 8) / 8
            return
        if base in exact_cnt:
            exact_cnt[base] += 1
            return
        exact_cnt[base] = exact_cnt.get(base, 0) + 1

    def parse_bk_by_day(bk):
        by_day = {d: [] for d in range(1, 32)}
        if not bk or bk in ('None', '0'):
            return by_day
        for m in re.finditer(r'\d{2}月(\d+(?:\.\d+)?)日(.+?)(?=\d{2}月|$)', bk):
            day = int(float(m.group(1)))
            if 1 <= day <= 31:
                by_day[day].append(m.group(2).strip())
        return by_day

    def calc_day_actual(statuses):
        cleaned = [normalize_status(s) for s in statuses if normalize_status(s)]
        if not cleaned:
            return 0.0
        bases = [split_status(s)[0] for s in cleaned]
        if any(b in ('休', '旷工', '产假') for b in bases):
            return 0.0
        unpaid_hours = 0.0
        for s in cleaned:
            base, hrs = split_status(s)
            if base in UNPAID_ATTENDANCE_TYPES:
                unpaid_hours += 8 if hrs is None else min(hrs, 8)
        return max(1 - min(unpaid_hours, 8) / 8, 0)

    def calc_unpaid_deduction(statuses):
        cleaned = [normalize_status(s) for s in statuses if normalize_status(s)]
        if not cleaned:
            return 0.0
        bases = [split_status(s)[0] for s in cleaned]
        if any(b in ('旷工', '产假') for b in bases):
            return 1.0
        unpaid_hours = 0.0
        for s in cleaned:
            base, hrs = split_status(s)
            if base in UNPAID_ATTENDANCE_TYPES:
                unpaid_hours += 8 if hrs is None else min(hrs, 8)
        return min(unpaid_hours, 8) / 8

    def calc_normal_detail(statuses):
        cleaned = [normalize_status(s) for s in statuses if normalize_status(s)]
        if not cleaned:
            return 0.0
        split = [split_status(s) for s in cleaned]
        bases = [base for base, _ in split]
        if any(base == '出差' and hrs is None for base, hrs in split):
            return 0.0

        # 部分假期当天按剩余工时计入“正常”。部分出差不从“正常”中扣减，
        # 出差仍单独进入出差统计及补贴口径。
        leave_hours = 0.0
        saw_partial_leave = False
        for base, hrs in split:
            if hrs is not None and base in NORMAL_PARTIAL_TYPES:
                saw_partial_leave = True
                leave_hours += min(hrs, 8)
        if saw_partial_leave:
            return max(8 - min(leave_hours, 8), 0) / 8

        main_base, _ = split[0]
        if main_base == '正常':
            return 1.0

        if any(b in ('休', '旷工', '产假', '缺卡', '补卡', '迟到', '外出') for b in bases):
            return 0.0
        return 0.0

    def calc_meal_deduct(statuses, day_actual):
        if day_actual <= 0:
            return 0.0
        deduct = 0.0
        for s in statuses:
            base, hrs = split_status(s)
            if base == '出差':
                units = 1.0 if hrs is None else _travel_stat_units_from_hours(hrs)
                deduct += units
            elif base in MEAL_DEDUCT_TYPES:
                deduct += 1.0 if hrs is None else min(hrs, 8) / 8
        return min(deduct, 1.0)

    payroll_rows = []

    for r in range(8, _last_employee_row(ws_w) + 1):
        name = str(ws_w.cell(r, 3).value or '').strip()
        if not name: continue

        has_special_rest_compensation = name in _POLICY.get('SPECIAL_REST_COMPENSATION_NAMES', ())

        exact_cnt = {s: 0 for s in COL_MAP.values()}
        partial_extra = {s: 0.0 for s in COL_MAP.values()}
        for s in PARTIAL_TYPES:
            partial_extra.setdefault(s, 0.0)
        normal_extra = 0.0
        day_statuses = {d: [] for d in range(1, 32)}

        # ==== 读考勤区 AF~BJ ====
        for d in range(1, 32):
            col = day_cols.get(d)
            if not col:
                continue
            v = str(ws_d.cell(r, col).value or '').strip()
            if not v: continue
            day_statuses[d].append(v)

        # ==== 读备注列 ====
        bk = str(ws_d.cell(r, remark_col).value or '').strip()
        bk_by_day = parse_bk_by_day(bk)
        for d, statuses in bk_by_day.items():
            day_statuses[d].extend(statuses)

        emp_meta = get_employee_meta(name)
        entry_date = parse_stat_entry_date(
            ws_w.cell(r, 4).value or ws_d.cell(r, 4).value
        )
        entry_day = (
            entry_date.day
            if entry_date and entry_date.year == stat_year and entry_date.month == stat_month
            else None
        )
        late_early_deduction = _calc_late_early_deduction(
            emp_meta.get('late_early_events') or [],
            entry_day=entry_day
        )
        schedule_info = _schedule_info_for_name(name, stat_year, stat_month, calendar_days) or {}
        normal_detail = 0.0
        normal_extra_box = [0.0]
        for statuses in day_statuses.values():
            for status in statuses:
                add_stat(status, exact_cnt, partial_extra, normal_extra_box)
        if schedule_info.get('rest_count') is not None:
            exact_cnt['休'] = int(schedule_info['rest_count'])
        exact_cnt['法定'] = legal_count_for_stats(name, ws_w.cell(r, 4).value or ws_d.cell(r, 4).value)
        daily_travel_subsidy_units = 0.0
        for d, statuses in day_statuses.items():
            daily_travel_subsidy_units += sum(status_travel_subsidy_units(status) for status in statuses)
        for d, statuses in day_statuses.items():
            normal_detail += calc_normal_detail(statuses)
        normal_extra = normal_extra_box[0]

        # ==== 写入 I~AA（详细分类，不变）====
        for col, status in COL_MAP.items():
            val = exact_cnt[status]
            if col == 9: val = normal_detail
            elif status in PARTIAL_TYPES: val += partial_extra[status]
            ws_w.cell(r, col, round(val, 2))

        c = lambda s: exact_cnt.get(s, 0)

        if deduction_col:
            ws_w.cell(r, deduction_col, round(late_early_deduction, 2))

        # ==== 加班补贴（不变）====
        overtime_subsidy = c('加班8点')*25 + c('加班9点')*45 + c('加班10点')*80
        ws_w.cell(r, overtime_subsidy_col, _truncate_fraction(overtime_subsidy))

        # ==== E/F/H：E为当月日历天数，F为实际出勤，H为工资核算天数 ====
        ws_w.cell(r, 5, calendar_days)

        actual_attendance_raw = 0.0
        unpaid_deduction = 0.0
        meal_deduct = 0.0
        for d in range(1, 32):
            day_actual = calc_day_actual(day_statuses[d])
            actual_attendance_raw += day_actual
            unpaid_deduction += calc_unpaid_deduction(day_statuses[d])
            meal_deduct += calc_meal_deduct(day_statuses[d], day_actual)
        # 与人工表原公式一致：餐补从已经保留两位小数的统计字段取扣减值，
        # 避免用原始小时精度计算后与 Excel 公式出现 0.01 的尾差。
        meal_deduct = sum(
            float(ws_w.cell(r, col).value or 0)
            for col in (14, 15, 16, 17, 19, 20)
        )
        active_through_month = calc_day_actual(day_statuses.get(calendar_days, [])) > 0

        try:
            scheduled_days = float(ws_w.cell(r, 7).value or ws_d.cell(r, 7).value or 0)
        except:
            scheduled_days = 0.0
        # 业务确认的普通双休人员不再沿用特殊休息日的排班天数。
        if name in STANDARD_DOUBLE_REST_NAMES:
            scheduled_days = 21.75
            ws_w.cell(r, 7, scheduled_days)
        else:
            dynamic_scheduled = _dynamic_scheduled_days(name, stat_year, stat_month, calendar_days)
            if dynamic_scheduled is not None:
                scheduled_days = float(dynamic_scheduled)
                ws_w.cell(r, 7, scheduled_days)
        if not scheduled_days:
            scheduled_days = _scheduled_days_for_name(name)
            ws_w.cell(r, 7, scheduled_days)
        if schedule_info.get('no_clock_required') or name in FULL_MONTH_INACTIVE_NAMES:
            actual_attendance_raw = 0.0
        elif schedule_info.get('actual_from_schedule') and (not emp_meta.get('is_separated') or active_through_month):
            actual_attendance_raw = max(float(scheduled_days or 0) - unpaid_deduction, 0.0)
        actual_attendance = round(actual_attendance_raw, 2)
        ws_w.cell(r, 6, actual_attendance)
        template = _payroll_template_for_name(name)
        if template and template[0] == 'fixed':
            payroll_days = template[1]
        elif template and template[0] == 'direct_f':
            payroll_days = actual_attendance_raw
        elif scheduled_days and abs(scheduled_days - 21.75) < 0.001:
            payroll_days = actual_attendance_raw * scheduled_days / 21
        elif template and template[0] == 'scale_21':
            g = scheduled_days or 21.75
            payroll_days = actual_attendance_raw * g / 21
        elif scheduled_days:
            payroll_days = actual_attendance_raw
        else:
            payroll_days = 0.0
        ws_w.cell(r, 8, round(payroll_days, 2))

        travel_units = float(emp_meta.get('travel_units') or 0)
        travel_subsidy = float(emp_meta.get('travel_subsidy') or 0)
        if travel_units > 0:
            if daily_travel_subsidy_units > 0:
                original_units = travel_units
                travel_units = min(travel_units, daily_travel_subsidy_units)
                if original_units > 0:
                    travel_subsidy = travel_subsidy * travel_units / original_units

        # ==== AC = 出差补贴：以出差申请为准，半天=25，全天=50，北上广深=100/天 ====
        bjgs_count = c('出差北上广深')
        if travel_subsidy > 0:
            ws_w.cell(r, travel_subsidy_col, _truncate_fraction(travel_subsidy))
        else:
            ws_w.cell(r, travel_subsidy_col, _truncate_fraction(daily_travel_subsidy_units*50 + bjgs_count*50))

        # ==== AD + AE = 餐补 + 全勤 ====
        position = emp_meta.get('position', '')
        group_text = f"{emp_meta.get('group', '')}{emp_meta.get('department', '')}"
        is_nb = any(nb in name for nb in ningbo_list) or name in NB_HAS_AD or has_special_rest_compensation or ('宁波' in group_text)

        # 全勤取消条件
        no_full = (c('事假') + partial_extra['事假'] > 0
                   or c('迟到') >= 1
                   or c('缺卡') + c('补卡') >= 4)

        if (has_special_rest_compensation
            or emp_meta.get('is_separated')
            or actual_attendance_raw <= 0
            or '主播' in position
            or '中控' in position
            # 月初之后入职者，当月即使已完成入职后的全部排班，也不发全勤奖。
            or entry_after_stat_month_start(ws_w.cell(r, 4).value or ws_d.cell(r, 4).value)):
            no_full = True

        if not no_full:
            for d in range(1, 32):
                col = day_cols.get(d)
                if not col:
                    continue
                v = str(ws_d.cell(r, col).value or '')
                if '已经离职' in v:
                    no_full = True
                    break

        if not is_nb and not no_full:
            paid = sum(c(s) + partial_extra.get(s, 0) for s in HANGZHOU_FULL_ATTENDANCE_PAID_TYPES)
            if paid * 8 > 8:
                no_full = True

        payroll_rows.append({
            'row': r,
            'name': name,
            'actual': actual_attendance_raw,
            'scheduled': scheduled_days,
            'meal_deduct': meal_deduct,
            'is_nb': is_nb,
            'no_full': no_full,
            'template': template,
        })

        if is_nb:
            # 宁波AD：150/应出勤 × (计入工资核算天数 - 出差 - 餐补扣减类带薪假)
            g = scheduled_days or 21.75
            ad = round(150 / g * max(payroll_days - meal_deduct, 0), 2)
            ws_w.cell(r, meal_allowance_col, ad)
            ws_w.cell(r, full_attendance_col, 150 if not no_full else 0)
        else:
            # 杭州：AD=0, AE=300
            ws_w.cell(r, meal_allowance_col, 0)
            ws_w.cell(r, full_attendance_col, 300 if not no_full else 0)

    payroll_bases = {}
    for item in payroll_rows:
        scheduled = item['scheduled']
        if not scheduled:
            continue
        key = round(float(scheduled), 2)
        # H列的折算口径以已写入F列的两位小数为准，和人工Excel公式保持一致。
        actual_for_payroll = round(float(item['actual'] or 0), 2)
        payroll_bases[key] = max(payroll_bases.get(key, 0.0), actual_for_payroll)

    for item in payroll_rows:
        r = item['row']
        scheduled = item['scheduled']
        actual = round(float(item['actual'] or 0), 2)
        template = item.get('template') or _payroll_template_for_name(item.get('name', ''))
        if template and template[0] == 'fixed':
            payroll_days = float(template[1] or 0)
            g = float(scheduled or 0)
        elif template and template[0] == 'direct_f':
            payroll_days = actual
            g = float(scheduled or 0)
        elif scheduled:
            g = float(scheduled)
            fixed_two_day_rest = _is_fixed_two_day_rest_schedule(
                item.get('name', ''), stat_year, stat_month, calendar_days
            )
            # 双休 H 列严格沿用人工 Excel：F * (G / 当月实际应出勤天数)。
            # G=21.75 是固定平均月基础天数；分母随月份变化（2026-07 为 23）。
            # 周五周六休等特殊双休：G 会显示该班制的当月实际应出勤，
            # 但 H 仍必须按 21.75 的双休平均月基础折算。
            payroll_g = 21.75 if fixed_two_day_rest else g
            if abs(g - 21.75) < 0.001:
                base = _double_rest_payroll_denominator(stat_year, stat_month)
            elif fixed_two_day_rest:
                base = g
            else:
                base = payroll_bases.get(round(g, 2)) or g
            payroll_days = 0.0 if base <= 0 else min(actual / base * payroll_g, payroll_g)
        else:
            g = 0.0
            payroll_days = actual
        payroll_days = round(payroll_days, 2)
        ws_w.cell(r, 8, payroll_days)

        if item['is_nb']:
            meal_g = g or 21.75
            ad = round(150 / meal_g * max(payroll_days - item['meal_deduct'], 0), 2)
            ws_w.cell(r, meal_allowance_col, ad)
            ws_w.cell(r, full_attendance_col, 150 if not item['no_full'] else 0)
        else:
            ws_w.cell(r, meal_allowance_col, 0)
            ws_w.cell(r, full_attendance_col, 300 if not item['no_full'] else 0)

    for attempt in range(5):
        try: wb_f.save(file_path); break
        except: time.sleep(2)


# ==================== 客服WiFi补贴 ====================
def _calc_subsidy(file_path, month=5):
    """循环所有客服人员，计算WiFi补贴写入AB列，一次调用全搞定"""
    if month in [1,3,5,7,8,10,12]: days_count = 31
    elif month == 2: days_count = 28
    else: days_count = 30

    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws_data = wb['月度数据']
    ws_src = wb['原始数据']
    ws_full = wb['全职']
    subsidy_col = next(
        (
            col for col in range(STAT_START_COL, ws_full.max_column + 1)
            if str(ws_full.cell(6, col).value or '').strip() == '加班补贴'
        ),
        28,
    )
    try:
        year = int(ws_full.cell(3, 3).value)
    except Exception:
        year = datetime.date.today().year

    # 找出全职sheet中员工名所在行
    name_to_row = {}
    for r in range(8, _last_employee_row(ws_full) + 1):
        n = str(ws_full.cell(r, 3).value or '').strip()
        if n: name_to_row[n] = r

    wb_out = openpyxl.load_workbook(file_path)
    ws_out = wb_out['全职']
    result = {}

    # 循环月度数据每个员工
    for r in range(5, ws_data.max_row + 1):
        name = str(ws_data.cell(r, 1).value or '').strip()
        grp = str(ws_data.cell(r, 2).value or '')
        department = str(ws_data.cell(r, 3).value or '')
        position = str(ws_data.cell(r, 5).value or '')
        is_customer_service = any('客服' in value for value in (grp, department, position))
        if not is_customer_service or any(excluded in name for excluded in _POLICY.get('WIFI_EXCLUSIONS', ())):
            continue

        # 统计这个员工有WiFi的晚班天数
        subsidy = 0
        for day in range(1, days_count + 1):
            col = 43 + day
            raw = ws_data.cell(r, col).value
            if not raw or '晚班' not in str(raw):
                continue

            date_str = f"{year % 100:02d}-{month:02d}-{day:02d}"
            has_wifi = False
            for sr in range(4, ws_src.max_row + 1):
                if str(ws_src.cell(sr, 1).value or '').strip() != name:
                    continue
                if date_str not in str(ws_src.cell(sr, 7).value or ''):
                    continue
                addr = str(ws_src.cell(sr, 11).value or '')
                if any(m in addr for m in WIFI_MACS) or any(k in addr for k in COMPANY_LOCATION_KEYWORDS):
                    has_wifi = True
                    break
            if has_wifi:
                subsidy += 1

        # 找到这个员工在全职sheet的行，写入加班补贴列。
        # 加班补贴列同时承载加班时点补贴与客服晚班 WiFi 补贴，不能在已有值上累加：
        # 否则重新生成草稿或补入新员工时会把原有客服补贴重复计算。
        for full_name, out_row in name_to_row.items():
            if full_name in name:
                overtime_8 = float(ws_out.cell(out_row, 22).value or 0)
                overtime_9 = float(ws_out.cell(out_row, 23).value or 0)
                overtime_10 = float(ws_out.cell(out_row, 24).value or 0)
                base_subsidy = overtime_8 * 25 + overtime_9 * 45 + overtime_10 * 80
                total_subsidy = round(base_subsidy + subsidy * 10, 2)
                ws_out.cell(out_row, subsidy_col, total_subsidy)
                result[name] = {
                    "subsidy_days": subsidy,
                    "wifi_subsidy": subsidy * 10,
                    "overtime_subsidy": base_subsidy,
                    "total": total_subsidy,
                }
                break

    for attempt in range(5):
        try: wb_out.save(file_path); break
        except: time.sleep(2)

    # Python 3.6 + Windows 下不显式关闭工作簿会锁住临时文件，
    # 影响 staging 测试清理；不改变任何计算结果。
    wb.close()
    wb_out.close()

    return result


# ==================== 统一入口 ====================
def _detect_month(file_path, fallback=5):
    try:
        wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
        ws = wb['全职']
        return int(ws.cell(3, 4).value)
    except Exception:
        return fallback


def run_attendance(file_path, config=None, ningbo_list=None, month=None):
    """全流程：考勤区判定 → 统计区计算 → 客服WiFi补贴，一次完成。

    参数:
        file_path : str - Excel文件路径（含 全职/月度数据/原始数据 三个sheet）
        config    : dict 可选 - 覆盖 DEFAULT_CONFIG 的配置项
        ningbo_list : list 可选 - 宁波员工名单，用于全勤/餐补判定
        month     : int 可选 - 月份（默认读取全职表头D3，失败时按5月）
    返回:
        dict - { 'main_judge': {...}, 'calc_subsidy': {...} }
    """
    if month is None:
        month = _detect_month(file_path)
    # 兼容旧版直接上传的全职结果/源表：若统计区还没有新扣款列，
    # 先完成一次结构迁移；已有新模板时保持不变。
    _patch_late_early_template(file_path)
    r1 = _main_judge(file_path, config)
    _calc_statistics(file_path, ningbo_list)
    r3 = _calc_subsidy(file_path, month)
    return {'main_judge': r1, 'calc_subsidy': r3}


def _parse_name_list(value):
    if not value:
        return None
    return [x.strip() for x in re.split(r'[,，;\n]+', value) if x.strip()]


def _cli():
    parser = argparse.ArgumentParser(description='运行全职考勤自动判定')
    parser.add_argument('input', help='输入Excel文件路径')
    parser.add_argument('-o', '--output', help='输出Excel文件路径；不填则原地覆盖输入文件')
    parser.add_argument('--month', type=int, help='统计月份；不填则读取全职表头D3')
    parser.add_argument('--ningbo', default='', help='额外宁波员工名单，逗号或换行分隔')
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    work_path = Path(args.output).resolve() if args.output else input_path
    if work_path != input_path:
        work_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, work_path)

    result = run_attendance(
        str(work_path),
        ningbo_list=_parse_name_list(args.ningbo),
        month=args.month,
    )
    print(json.dumps({'ok': True, 'output': str(work_path), 'result': result}, ensure_ascii=False))


if __name__ == '__main__':
    _cli()
