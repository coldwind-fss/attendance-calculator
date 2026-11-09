import copy
import re
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter, range_boundaries


INSERT_COL = 28  # AB：放在“迟到”之后
LABEL = '迟到/早退扣款'


def _update_attendance_formula_ranges(worksheet):
    changed = False
    for row in worksheet.iter_rows():
        for cell in row:
            value = cell.value
            if not isinstance(value, str) or not value.startswith('='):
                continue
            updated = re.sub(r'(?<![A-Z])\$AF(\d+)', r'$AG\1', value)
            updated = re.sub(r'(?<![A-Z])\$BK(\d+)', r'$BL\1', updated)
            if updated != value:
                cell.value = updated
                changed = True
    return changed


def patch_template(path):
    path = Path(path).resolve()
    workbook = openpyxl.load_workbook(path)
    worksheet = workbook['全职']
    if str(worksheet.cell(6, INSERT_COL).value or '').strip() == LABEL:
        changed = _update_attendance_formula_ranges(worksheet)
        if changed:
            workbook.save(path)
        return changed

    merges = [str(item) for item in worksheet.merged_cells.ranges]
    for merged in merges:
        worksheet.unmerge_cells(merged)

    dimension_snapshot = {}
    for col in range(1, worksheet.max_column + 1):
        letter = get_column_letter(col)
        dimension = worksheet.column_dimensions[letter]
        dimension_snapshot[col] = (dimension.width, dimension.hidden)

    worksheet.insert_cols(INSERT_COL, 1)

    source_col = INSERT_COL - 1  # AA，沿用迟到列样式
    for row in range(1, worksheet.max_row + 1):
        source = worksheet.cell(row, source_col)
        target = worksheet.cell(row, INSERT_COL)
        if source.has_style:
            target._style = copy.copy(source._style)
        target.number_format = source.number_format
        target.alignment = copy.copy(source.alignment)
        target.fill = copy.copy(source.fill)
        target.font = copy.copy(source.font)
        target.border = copy.copy(source.border)
        target.protection = copy.copy(source.protection)

    worksheet.cell(6, INSERT_COL, LABEL)
    worksheet.cell(7, INSERT_COL, None)

    for old_col, (width, hidden) in dimension_snapshot.items():
        new_col = old_col + 1 if old_col >= INSERT_COL else old_col
        dimension = worksheet.column_dimensions[get_column_letter(new_col)]
        dimension.width = width
        dimension.hidden = hidden
    worksheet.column_dimensions[get_column_letter(INSERT_COL)].width = dimension_snapshot[source_col][0]

    for merged in merges:
        min_col, min_row, max_col, max_row = range_boundaries(merged)
        if min_col >= INSERT_COL:
            min_col += 1
            max_col += 1
        elif max_col >= INSERT_COL:
            max_col += 1
        worksheet.merge_cells(
            f'{get_column_letter(min_col)}{min_row}:'
            f'{get_column_letter(max_col)}{max_row}'
        )
    worksheet.merge_cells('AB6:AB7')
    _update_attendance_formula_ranges(worksheet)
    workbook.save(path)
    return True


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('path')
    args = parser.parse_args()
    print({'patched': patch_template(args.path), 'path': str(Path(args.path).resolve())})
