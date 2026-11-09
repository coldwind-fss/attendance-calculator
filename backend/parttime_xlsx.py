"""Read live-session workbooks in the formats supported by the legacy engine."""

import re
from datetime import datetime, timedelta
from math import isfinite
from uuid import uuid4
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from .jobs import JobFailure
from .parttime_hours import round_hours


STORES = {
    "official": ("抖音官方旗舰店", ("官方旗舰", "官旗", "官抖")),
    "sports": ("抖音运动旗舰店", ("运动旗舰", "运动")),
    "outdoor": ("抖音户外旗舰店", ("户外", "越野")),
    "tmall": ("天猫", ("天猫",)),
}
START = ("开播时间", "直播开始时间")
SECONDS = {"直播时长（秒）", "直播时长(秒)", "直播间时长（秒）"}
MINUTES = ("开播时长（分钟）", "开播时长(分钟)", "直播时长（分钟）", "直播时长(分钟)",
           "直播间时长（分钟）", "直播间时长(分钟)")
TITLE = ("直播间标题", "场次信息", "标题")
DATE_TIME = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?")


def open_workbook(path):
    try:
        return load_workbook(path, read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError):
        raise JobFailure("无法解析所选工作簿，请检查格式和工作表") from None


def sheet_names(path):
    workbook = open_workbook(path)
    try:
        return workbook.sheetnames
    finally:
        workbook.close()


def infer_store(filename):
    return next((store for store, (_, words) in STORES.items() if any(word in filename for word in words)), "official")


def number(value):
    try:
        parsed = float(value.replace(",", "") if isinstance(value, str) else value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if isfinite(parsed) else 0


def session_start(value):
    if isinstance(value, datetime):
        # ExcelJS treated a date cell as UTC, then formatted it in Asia/Shanghai.
        return value + timedelta(hours=8)
    match = DATE_TIME.match(str(value or "").strip().replace("/", "-"))
    if not match:
        return None
    year, month, day, hours, minutes, seconds = (int(item or 0) for item in match.groups())
    if year < 100:
        year += 1900
    offset, month = divmod(month - 1, 12)
    return datetime(year + offset, month + 1, 1) + timedelta(days=day - 1, hours=hours, minutes=minutes, seconds=seconds)


def header_index(headers, names):
    for name in names:
        for index, label in enumerate(headers):
            if name in label:
                return index
    return None


def cell(row, index):
    return row[index] if index is not None and index < len(row) else None


def read_sessions(path, store):
    workbook = open_workbook(path)
    try:
        rows = enumerate(workbook.worksheets[0].values, 1)
        for _, raw in rows:
            headers = tuple(str(value or "").strip() for value in raw)
            start_column = header_index(headers, START)
            if start_column is not None:
                break
        else:
            return []

        seconds_column = next((index for index, label in enumerate(headers) if label in SECONDS), None)
        minutes_column = header_index(headers, MINUTES)
        title_column = header_index(headers, TITLE)
        output = []
        for row_number, row in rows:
            start = session_start(cell(row, start_column))
            if start is None:
                continue
            hours = number(cell(row, seconds_column)) / 3600
            if hours <= 0:
                hours = number(cell(row, minutes_column)) / 60
            if hours <= 0:
                continue
            end = start + timedelta(milliseconds=int(hours * 3_600_000))
            output.append({"id": uuid4().hex, "date": start.date().isoformat(), "store_id": store,
                           "store_name": STORES[store][0], "title": str(cell(row, title_column) or "").strip(),
                           "start": start.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
                           "hours": round_hours(hours, 3), "source_row": row_number})
        return output
    finally:
        workbook.close()
