from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


STATISTICS = (
    "姓名", "入职时间", "合计", "实际出勤", "应出勤", "计入工资核算天数", "正常", "缺卡", "补卡", "事假",
    "病假", "年假", "司龄", "调休", "婚假", "产假", "丧假", "出差", "外出", "加班8点", "加班9点",
    "加班10点", "休", "法定", "迟到", "迟到/早退扣款", "加班补贴", "出差补贴", "餐补", "全勤",
)


def write_blank_template(path):
    book = Workbook()
    sheet = book.active
    sheet.title = "全职"
    sheet["C2"] = "全职考勤"
    sheet["AG5"] = "每日考勤"
    sheet.freeze_panes = "E8"
    header_fill = PatternFill("solid", fgColor="243657")
    for column, label in enumerate(("", "序号", *STATISTICS, *range(1, 32), "备注"), 1):
        cell = sheet.cell(6, column, label)
        cell.fill = header_fill
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        width = 9 if column >= 33 else 13
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.column_dimensions["BL"].width = 24
    sheet.row_dimensions[6].height = 28
    sheet.cell(7, 4).number_format = "yyyy-mm-dd"
    book.create_sheet("月度数据")
    book.create_sheet("原始数据")
    book.save(path)
