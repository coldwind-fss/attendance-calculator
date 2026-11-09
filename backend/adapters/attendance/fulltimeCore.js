import ExcelJS from 'exceljs';

const STAT_START_COL = 3;
const FIRST_DATA_ROW = 8;

export async function parseFulltimeOutputWorkbook(buffer) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const ws = wb.getWorksheet('全职') || wb.worksheets[0];
  if (!ws) throw new Error('处理后的文件中未找到全职 Sheet');

  const year = asNumber(cellValue(ws.getCell(3, 3))) || '';
  const month = asNumber(cellValue(ws.getCell(3, 4))) || '';
  const attendanceStartCol = findAttendanceStartColumn(ws);
  const statColumns = buildStatColumns(ws, attendanceStartCol - 1);
  const attendanceColumns = buildAttendanceColumns(ws, attendanceStartCol);
  const remarkCol = findRemarkColumn(ws, attendanceColumns);
  const statisticsRows = [];
  const attendanceRows = [];

  for (let rowNumber = FIRST_DATA_ROW; rowNumber <= ws.rowCount; rowNumber++) {
    const name = displayValue(ws.getCell(rowNumber, 3));
    if (!name) continue;

    const statRow = { row_number: rowNumber };
    for (const column of statColumns) {
      statRow[column.key] = displayValue(ws.getCell(rowNumber, column.col));
    }
    statisticsRows.push(statRow);

    attendanceRows.push({
      row_number: rowNumber,
      name,
      days: attendanceColumns.map(column => ({
        day: column.day,
        weekday: column.weekday,
        status: displayValue(ws.getCell(rowNumber, column.col))
      })),
      remark: remarkCol ? displayValue(ws.getCell(rowNumber, remarkCol)) : ''
    });
  }

  return {
    workbook: {
      sheet_name: ws.name,
      year,
      month
    },
    summary: buildSummary(statisticsRows),
    statistics: {
      columns: statColumns.map(({ key, label }) => ({ key, label })),
      rows: statisticsRows
    },
    attendance: {
      columns: attendanceColumns.map(({ day, weekday }) => ({ day, weekday })),
      rows: attendanceRows
    }
  };
}

function findAttendanceStartColumn(ws) {
  for (let col = STAT_START_COL; col <= Math.min(ws.columnCount || 100, 100); col++) {
    const day = asNumber(cellValue(ws.getCell(6, col)));
    if (Number.isInteger(day) && day >= 1 && day <= 31) return col;
  }
  return 32;
}

function buildStatColumns(ws, endCol) {
  const columns = [];
  const usedKeys = new Map();
  for (let col = STAT_START_COL; col <= endCol; col++) {
    const label = displayValue(ws.getCell(6, col)) || `列${col}`;
    const baseKey = slugStatLabel(label, col);
    const count = (usedKeys.get(baseKey) || 0) + 1;
    usedKeys.set(baseKey, count);
    columns.push({
      col,
      label,
      key: count > 1 ? `${baseKey}_${count}` : baseKey
    });
  }
  return columns;
}

function buildAttendanceColumns(ws, startCol) {
  const columns = [];
  const maxCol = Math.min(ws.columnCount || 100, 100);
  for (let col = startCol; col <= maxCol; col++) {
    const day = asNumber(cellValue(ws.getCell(6, col)));
    if (!day || day < 1 || day > 31) continue;
    columns.push({
      col,
      day,
      weekday: displayValue(ws.getCell(7, col))
    });
  }
  return columns;
}

function findRemarkColumn(ws, attendanceColumns) {
  const maxCol = Math.min(ws.columnCount || 100, 100);
  const startCol = attendanceColumns.length ? attendanceColumns[0].col : 32;
  for (const row of [5, 6, 7]) {
    for (let col = startCol; col <= maxCol; col++) {
      if (displayValue(ws.getCell(row, col)) === '备注') return col;
    }
  }
  if (attendanceColumns.length) {
    return Math.max(...attendanceColumns.map(column => column.col)) + 1;
  }
  return 0;
}

function buildSummary(rows) {
  return {
    people_count: rows.length,
    actual_attendance_total: round(sum(rows, 'actual_attendance'), 2),
    payroll_days_total: round(sum(rows, 'payroll_days'), 2),
    overtime_subsidy_total: round(sum(rows, 'overtime_subsidy'), 2),
    travel_subsidy_total: round(sum(rows, 'travel_subsidy'), 2),
    meal_allowance_total: round(sum(rows, 'meal_allowance'), 2),
    full_attendance_total: round(sum(rows, 'full_attendance'), 2),
    late_early_deduction_total: round(sum(rows, 'late_early_deduction'), 2)
  };
}

function slugStatLabel(label, col) {
  const map = {
    '姓名': 'name',
    '入职时间': 'entry_date',
    '合计': 'total',
    '实际出勤': 'actual_attendance',
    '应出勤': 'scheduled_attendance',
    '计入工资核算天数': 'payroll_days',
    '正常': 'normal',
    '缺卡': 'missing_punch',
    '补卡': 'punch_correction',
    '事假': 'personal_leave',
    '病假': 'sick_leave',
    '年假': 'annual_leave',
    '司龄': 'seniority_leave',
    '调休': 'comp_leave',
    '婚假': 'marriage_leave',
    '产假': 'maternity_leave',
    '丧假': 'bereavement_leave',
    '出差': 'business_trip',
    '外出': 'outing',
    '加班8点': 'overtime_8',
    '加班9点': 'overtime_9',
    '加班10点': 'overtime_10',
    '休': 'rest',
    '法定': 'legal_holiday',
    '迟到': 'late',
    '迟到/早退扣款': 'late_early_deduction',
    '加班补贴': 'overtime_subsidy',
    '出差补贴': 'travel_subsidy',
    '餐补': 'meal_allowance',
    '全勤': 'full_attendance'
  };
  return map[label] || `col_${col}`;
}

function displayValue(cell) {
  const value = cellValue(cell);
  if (value == null) return '';
  if (value instanceof Date) return formatDate(value);
  if (typeof value === 'number') return round(value, 6);
  return String(value).trim();
}

function cellValue(cell) {
  const value = cell?.value;
  if (value == null) return '';
  if (value instanceof Date) return value;
  if (typeof value === 'object') {
    if ('result' in value && value.result != null) return value.result;
    if ('text' in value && value.text != null) return value.text;
    if ('richText' in value && Array.isArray(value.richText)) {
      return value.richText.map(part => part.text || '').join('');
    }
    if ('hyperlink' in value && 'text' in value) return value.text;
    return cell.text && cell.text !== '[object Object]' ? cell.text : '';
  }
  return value;
}

function asNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const n = Number(String(value || '').trim());
  return Number.isFinite(n) ? n : 0;
}

function sum(rows, key) {
  return rows.reduce((total, row) => total + asNumber(row[key]), 0);
}

function round(value, digits) {
  const m = 10 ** digits;
  return Math.round((Number(value) || 0) * m) / m;
}

function formatDate(value) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}
