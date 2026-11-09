import ExcelJS from 'exceljs';
import companyPolicy from '../../company_policy.cjs';

export const STORES = {
  official: { name: '抖音官方旗舰店', short: '官旗', keywords: ['官方旗舰', '官旗', '官抖'] },
  sports: { name: '抖音运动旗舰店', short: '运动旗舰', keywords: ['运动旗舰', '运动'] },
  outdoor: { name: '抖音户外旗舰店', short: '户外/越野', keywords: ['户外', '越野'] },
  tmall: { name: '天猫', short: '天猫', keywords: ['天猫'] }
};

const ROLE_RATES = { '主播': 200, '中控': 50 };
const PERSON_RATES = companyPolicy.parttime?.PERSON_RATES ?? {};
const ALIASES = companyPolicy.parttime?.ALIASES ?? {};
const BAD_NAMES = new Set(['', '休息', '长休', '年假', '病假', '节假日', 'OT', '无', '空', '请假', '全员会议']);
const PERSON_REAL_NAMES = companyPolicy.parttime?.PERSON_REAL_NAMES ?? {};
const DISPLAY_ALIASES = companyPolicy.parttime?.DISPLAY_ALIASES ?? {};
const SPORTS_XHS_ONLY_PEOPLE = new Set(companyPolicy.parttime?.SPORTS_XHS_ONLY_PEOPLE || []);
const SETTLEMENT_PROTECTION_TARGETS = companyPolicy.parttime?.SETTLEMENT_PROTECTION_TARGETS ?? [];
const ATTENDANCE_STORE_COLUMNS = [
  { id: 'official', label: '抖音（官旗）' },
  { id: 'sports_dou2', label: '小红书（抖二）' },
  { id: 'sports_video', label: '小红书（视频号）' },
  { id: 'sports_xhs', label: '小红书（小红书）' },
  { id: 'tmall', label: '天猫' },
  { id: 'shooting', label: '拍摄开票' }
];
const DEFAULT_SETTLEMENT_ORDER = companyPolicy.parttime?.DEFAULT_SETTLEMENT_ORDER ?? [];
const DEFAULT_PAYROLL_ORDER = companyPolicy.parttime?.DEFAULT_PAYROLL_ORDER ?? [];
const PRIMARY_STORE_COLUMNS = [
  { id: 'official', label: '官旗' },
  { id: 'sports', label: '运动旗舰' },
  { id: 'tmall', label: '天猫' }
];
const PRIMARY_STORE_IDS = new Set(PRIMARY_STORE_COLUMNS.map(store => store.id));
const CHANNEL_SHEET_CONFIG = [
  { id: 'official', label: '官旗', match: name => name.includes('官') },
  { id: 'sports', label: '小红书（运动）', match: name => name.includes('运动') },
  { id: 'tmall', label: '天猫', match: name => name === '天猫' }
];
const DEFAULT_WARNING_DIFF_HOURS = 0.25;

export async function workbookSheets(buffer) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  return wb.worksheets.map(sheet => sheet.name);
}

export function inferStoreId(text = '', fallback = 'official') {
  const raw = String(text || '');
  for (const [id, info] of Object.entries(STORES)) {
    if (info.keywords.some(keyword => raw.includes(keyword))) return id;
  }
  return fallback;
}

export async function parseSchedule(buffer, sheetName) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const ws = wb.getWorksheet(sheetName) || wb.worksheets[0];
  const rowCount = ws.rowCount;
  const colCount = ws.columnCount;
  const records = [];
  const anchorByCell = new Map();
  let dateCols = new Map();
  let currentStoreId = 'official';

  for (let r = 1; r <= rowCount; r++) {
    const rowDates = new Map();
    for (let c = 1; c <= colCount; c++) {
      const parsed = asDate(cellValue(ws, r, c));
      if (parsed) rowDates.set(c, parsed);
    }
    if (rowDates.size >= 3) {
      dateCols = rowDates;
      continue;
    }

    const storeHint = firstText([cellValue(ws, r, 1), cellValue(ws, r, 2)]);
    if (storeHint && storeHint.includes('店')) currentStoreId = inferStoreId(storeHint, currentStoreId);

    const role = normalizeRole(cellValue(ws, r, 5));
    if (!['主播', '中控'].includes(role) || !dateCols.size) continue;

    const band = cleanText(cellValue(ws, r, 3)) || '未命名场次';
    const defaultInterval = parseIntervalFromText(band);

    for (const [c, day] of dateCols.entries()) {
      const raw = cleanText(cellValue(ws, r, c));
      if (!raw || BAD_NAMES.has(raw)) continue;
      const sourceCell = `${colLetter(c)}${r}`;

      if (!['official', 'sports'].includes(currentStoreId)) continue;

      if (role === '主播') {
        const segments = parsePeopleIntervals(raw, defaultInterval);
        anchorByCell.set(`${r + 1}:${c}`, segments.map(item => [item.start, item.end, item.person]));
        for (const item of segments) {
          addSchedule(records, day, currentStoreId, role, item.person, item.start, item.end, band, '', sourceCell, raw);
        }
      } else {
        const anchors = anchorByCell.get(`${r}:${c}`) || [];
        const [start, end] = controlIntervalFromText(raw, anchors, defaultInterval);
        const people = extractPeopleWithoutTimes(raw);
        for (const person of people) {
          addSchedule(records, day, currentStoreId, role, person, start, end, band, [...new Set(anchors.map(x => x[2]))].join('/'), sourceCell, raw);
        }
      }
    }
  }

  return records;
}

export async function parseTmallSchedule(buffer, sheetName) {
  if (!buffer) return [];
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const ws = wb.getWorksheet(sheetName) || wb.worksheets[0];
  const records = [];

  for (let r = 1; r <= ws.rowCount; r++) {
    if (cleanText(cellValue(ws, r, 1)) !== '日期') continue;
    const dates = new Map();
    for (let c = 2; c <= Math.min(8, ws.columnCount); c++) {
      const day = asDate(cellValue(ws, r, c));
      if (day) dates.set(c, day);
    }
    if (!dates.size) continue;

    const blockEnd = nextTmallDateRow(ws, r + 1);
    for (let tr = r + 1; tr < blockEnd - 1; tr++) {
      const anchorLabel = cleanText(cellValue(ws, tr + 1, 1));
      const controlLabel = cleanText(cellValue(ws, tr + 2, 1));
      if (!anchorLabel.includes('主播') || !controlLabel.includes('场控')) continue;
      const band = cleanText(cellValue(ws, tr, 1)) || '天猫场次';
      for (const [c, day] of dates.entries()) {
        const interval = parseIntervalFromText(cellValue(ws, tr, c));
        if (!interval) continue;
        const [start, end] = interval;
        const anchorRaw = cleanText(cellValue(ws, tr + 1, c));
        const controlRaw = cleanText(cellValue(ws, tr + 2, c));
        for (const person of extractPeopleWithoutTimes(anchorRaw)) {
          addSchedule(records, day, 'tmall', '主播', person, start, end, band, '', `${colLetter(c)}${tr + 1}`, anchorRaw);
        }
        for (const person of extractPeopleWithoutTimes(controlRaw)) {
          addSchedule(records, day, 'tmall', '中控', person, start, end, band, '', `${colLetter(c)}${tr + 2}`, controlRaw);
        }
      }
      tr += 2;
    }
  }

  return records;
}

function nextTmallDateRow(ws, startRow) {
  for (let r = startRow; r <= ws.rowCount; r++) {
    if (cleanText(cellValue(ws, r, 1)) === '日期') return r;
  }
  return ws.rowCount + 1;
}

export async function parseBackend(buffer, storeId, sheetName) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const ws = wb.getWorksheet(sheetName) || wb.worksheets[0];
  const rows = [];
  const rowCount = ws.rowCount;
  const colCount = ws.columnCount;
  for (let r = 1; r <= rowCount; r++) {
    const row = [];
    for (let c = 1; c <= colCount; c++) row.push(cellValue(ws, r, c));
    rows.push(row);
  }
  let headerIndex = -1;
  let headers = [];

  for (let i = 0; i < rows.length; i++) {
    const row = rows[i].map(cleanText);
    const joined = row.join('|');
    if (joined.includes('开播时间') || joined.includes('直播开始时间')) {
      headerIndex = i;
      headers = row;
      break;
    }
  }
  if (headerIndex < 0) return [];

  const startIdx = findHeader(headers, ['开播时间', '直播开始时间']);
  const secondsIdx = findDurationSecondsHeader(headers);
  const minutesIdx = findHeader(headers, ['开播时长（分钟）', '开播时长(分钟)', '直播时长（分钟）', '直播时长(分钟)', '直播间时长（分钟）', '直播间时长(分钟)']);
  const titleIdx = findHeader(headers, ['直播间标题', '场次信息', '标题']);
  const out = [];

  for (let i = headerIndex + 1; i < rows.length; i++) {
    const row = rows[i];
    const start = parseDateTime(row[startIdx]);
    if (!start) continue;
    let hours = 0;
    if (secondsIdx >= 0) hours = parseNumber(row[secondsIdx]) / 3600;
    if ((!hours || hours <= 0) && minutesIdx >= 0) hours = parseNumber(row[minutesIdx]) / 60;
    if (!hours || hours <= 0) continue;
    const end = new Date(start.getTime() + hours * 3600000);
    out.push({
      id: cryptoId(),
      date: formatDate(start),
      store_id: storeId,
      store_name: STORES[storeId]?.name || storeId,
      title: titleIdx >= 0 ? cleanText(row[titleIdx]) : '',
      start: formatDateTime(start),
      end: formatDateTime(end),
      hours: round(hours, 3),
      source_row: i + 1
    });
  }
  return out;
}

export async function parseOperationWorkbook(buffer) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const out = [];

  for (const ws of wb.worksheets) {
    const storeId = inferStoreId(ws.name, '');
    if (!storeId) continue;
    const rows = [];
    for (let r = 1; r <= ws.rowCount; r++) {
      const row = [];
      for (let c = 1; c <= ws.columnCount; c++) row.push(cellValue(ws, r, c));
      rows.push(row);
    }

    let headerIndex = -1;
    let headers = [];
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i].map(cleanText);
      const joined = row.join('|');
      if (joined.includes('开播时间') || joined.includes('直播开始时间')) {
        headerIndex = i;
        headers = row;
        break;
      }
    }
    if (headerIndex < 0) continue;

    const startIdx = findHeader(headers, ['开播时间', '直播开始时间']);
    const secondsIdx = findDurationSecondsHeader(headers);
    const minutesIdx = findHeader(headers, ['开播时长（分钟）', '开播时长(分钟)', '直播时长（分钟）', '直播时长(分钟)', '直播间时长（分钟）', '直播间时长(分钟)']);
    const hoursIdx = findHeader(headers, ['直播时长(时)', '直播时长（时）', '时长(时)', '时长（时）']);
    const totalRoleIdx = headers.findIndex(header => header === '主播' || header === '中控');
    const personColumns = headers
      .map((header, index) => ({ header: canonicalPerson(header), index }))
      .filter(item => item.index !== startIdx && item.index !== secondsIdx && item.index !== minutesIdx && item.index !== hoursIdx)
      .filter(item => item.header && !['主播', '中控', '标题', '开播时间', '直播开始时间', '直播结束时间', '结束时间', '直播时长', '时长'].some(word => item.header.includes(word)))
      .filter(item => totalRoleIdx < 0 || item.index < totalRoleIdx);
    const personColumnIndexes = new Set(personColumns.map(column => column.index));
    const structureColumnIndexes = new Set([startIdx, secondsIdx, minutesIdx, hoursIdx].filter(index => index >= 0));
    let lastSessionMeta = null;

    for (let i = headerIndex + 1; i < rows.length; i++) {
      const row = rows[i];
      let start = parseDateTime(row[startIdx]);
      let sessionHours = 0;
      if (hoursIdx >= 0) sessionHours = parseNumber(row[hoursIdx]);
      if ((!sessionHours || sessionHours <= 0) && secondsIdx >= 0) sessionHours = parseNumber(row[secondsIdx]) / 3600;
      if ((!sessionHours || sessionHours <= 0) && minutesIdx >= 0) sessionHours = parseNumber(row[minutesIdx]) / 60;
      let end = null;

      if (start) {
        if (!sessionHours || sessionHours <= 0) continue;
        end = new Date(start.getTime() + sessionHours * 3600000);
        lastSessionMeta = { start, end, sessionHours };
      } else if (lastSessionMeta && isOperationContinuationRow(row, personColumnIndexes, structureColumnIndexes)) {
        start = lastSessionMeta.start;
        end = lastSessionMeta.end;
        sessionHours = lastSessionMeta.sessionHours;
      } else {
        continue;
      }

      for (const column of personColumns) {
        const hours = parseNumber(row[column.index]);
        if (!hours || hours <= 0) continue;
        out.push({
          id: cryptoId(),
          date: formatDate(start),
          store_id: storeId,
          store_name: STORES[storeId]?.name || storeId,
          person: column.header,
          start: formatDateTime(start),
          end: formatDateTime(end),
          session_hours: round(sessionHours, 3),
          operation_hours: round(hours, 3),
          source_sheet: ws.name,
          source_row: i + 1,
          source_column: column.index + 1
        });
      }
    }
  }

  return out;
}

export async function parseAttendanceOutputWorkbook(buffer) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const ws = wb.getWorksheet('兼职工资');
  if (!ws) return null;

  const lastColumn = Math.min(34, ws.columnCount);
  const headerRows = [1, 2].map(rowNumber => {
    const values = [];
    for (let c = 1; c <= lastColumn; c++) values.push(cellDisplayValue(ws.getCell(rowNumber, c)));
    return values;
  });

  const tableRows = [];
  const payroll = [];
  const stores = ATTENDANCE_STORE_COLUMNS.map(store => ({ ...store }));
  let totalRow = null;

  for (let r = 3; r <= ws.rowCount; r++) {
    const role = cleanText(cellDisplayValue(ws.getCell(r, 1)));
    const alias = canonicalPerson(cellDisplayValue(ws.getCell(r, 3)));
    const name = cleanText(cellDisplayValue(ws.getCell(r, 4)));
    if (!role && !alias && !name) continue;

    const cells = [];
    for (let c = 1; c <= lastColumn; c++) cells.push(cellDisplayValue(ws.getCell(r, c)));
    const row = {
      role,
      index: cellDisplayValue(ws.getCell(r, 2)),
      alias,
      name,
      cells,
      stores: buildAuthorityStores(ws, r),
      total_hours: round(cellNumber(ws.getCell(r, 30)), 2),
      total_amount: round(cellNumber(ws.getCell(r, 31)), 2),
      tax: round(cellNumber(ws.getCell(r, 32)), 2),
      net: round(cellNumber(ws.getCell(r, 33)), 2),
      rate: round(cellNumber(ws.getCell(r, 34)), 2)
    };
    tableRows.push(row);
    if (role === '合计' || alias === '合计') {
      totalRow = row;
      continue;
    }
    for (const store of stores) {
      const item = row.stores[store.id] || { hours: 0, amount: 0 };
      if (!item.hours && !item.amount) continue;
      payroll.push({
        store_id: store.id,
        store_name: store.label,
        role: row.role,
        person: row.alias,
        planned_hours: '',
        actual_hours: item.hours,
        rate: row.rate,
        amount: item.amount
      });
    }
  }

  const nonTotalRows = tableRows.filter(row => row.role !== '合计' && row.alias !== '合计');
  const summary = totalRow || {
    total_hours: round(sum(nonTotalRows, 'total_hours'), 2),
    total_amount: round(sum(nonTotalRows, 'total_amount'), 2)
  };

  return {
    source: '兼职工资',
    authoritative: true,
    headerRows,
    columnCount: lastColumn,
    stores,
    rows: tableRows,
    payroll,
    stats: {
      people_count: nonTotalRows.length,
      actual_person_hours: round(summary.total_hours, 2),
      payroll_amount: round(summary.total_amount, 2)
    }
  };
}

export async function parseSettlementOutputWorkbook(buffer) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const ws = wb.getWorksheet('结算');
  if (!ws) return null;

  const rows = [];
  for (let r = 1; r <= ws.rowCount; r++) {
    const role = cleanText(cellDisplayValue(ws.getCell(r, 1)));
    const alias = canonicalPerson(cellDisplayValue(ws.getCell(r, 2)));
    const name = cleanText(cellDisplayValue(ws.getCell(r, 3)));
    if (!role && !alias && !name) continue;
    if (role === '岗位' || alias === '花名' || name === '姓名') continue;

    rows.push({
      role,
      alias,
      name,
      order: rows.length
    });
  }

  return {
    source: '结算',
    rows,
    people: rows.filter(row => row.role !== '合计' && row.alias !== '合计')
  };
}

export async function parseChannelOutputTables(buffer) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buffer);
  const tables = [];

  for (const config of CHANNEL_SHEET_CONFIG) {
    const ws = wb.worksheets.find(sheet => config.match(cleanText(sheet.name)));
    if (!ws) continue;
    const columnCount = ws.columnCount;
    const headerRows = [];
    const rows = [];
    const headers = [];
    for (let c = 1; c <= columnCount; c++) headers.push(cleanText(cellDisplayValue(ws.getCell(1, c))));
    const startIdx = findHeader(headers, ['开播时间', '直播开始时间']);
    const secondsIdx = findDurationSecondsHeader(headers);
    const minutesIdx = findHeader(headers, ['开播时长（分钟）', '开播时长(分钟)', '直播时长（分钟）', '直播时长(分钟)', '直播间时长（分钟）', '直播间时长(分钟)']);
    const hoursIdx = findHeader(headers, ['直播时长(时)', '直播时长（时）', '时长(时)', '时长（时）']);
    const anchorIdx = headers.findIndex(header => header === '主播');
    const controlIdx = headers.findIndex(header => header === '中控');

    for (let r = 1; r <= ws.rowCount; r++) {
      const cells = [];
      for (let c = 1; c <= columnCount; c++) cells.push(cellDisplayValue(ws.getCell(r, c)));
      if (r === 1) {
        headerRows.push(cells);
      } else {
        const start = startIdx >= 0 ? parseDateTime(cellValue(ws, r, startIdx + 1)) : null;
        let backendHours = 0;
        if (hoursIdx >= 0) backendHours = parseNumber(cellValue(ws, r, hoursIdx + 1));
        if ((!backendHours || backendHours <= 0) && secondsIdx >= 0) backendHours = parseNumber(cellValue(ws, r, secondsIdx + 1)) / 3600;
        if ((!backendHours || backendHours <= 0) && minutesIdx >= 0) backendHours = parseNumber(cellValue(ws, r, minutesIdx + 1)) / 60;
        const end = start && backendHours > 0 ? new Date(start.getTime() + backendHours * 3600000) : null;
        rows.push({
          cells,
          meta: {
            rowNumber: r,
            date: start ? formatDate(start) : '',
            start: start ? formatDateTime(start) : '',
            end: end ? formatDateTime(end) : '',
            backend_hours: round(backendHours, 3),
            operation_anchor_hours: anchorIdx >= 0 ? round(parseNumber(cellValue(ws, r, anchorIdx + 1)), 3) : '',
            operation_control_hours: controlIdx >= 0 ? round(parseNumber(cellValue(ws, r, controlIdx + 1)), 3) : ''
          }
        });
      }
    }

    tables.push({
      store_id: config.id,
      store_name: STORES[config.id]?.name || config.label,
      label: config.label,
      source_sheet: ws.name,
      authoritative: true,
      columnCount,
      headerRows,
      rows,
      baseColumnCount: columnCount
    });
  }

  return tables;
}

export function analyze(scheduleRows, backendRows, operationRows = [], options = {}) {
  const warningDiffHours = normalizeWarningDiffHours(options.warningDiffHours);
  const timeCalculationMode = normalizeTimeCalculationMode(options.timeCalculationMode);
  const schedule = scheduleRows.map(row => ({ ...row }));
  const backend = backendRows.map(row => ({ ...row }));
  const operations = operationRows.map(row => ({ ...row }));
  const hasOperationSource = operations.length > 0;
  const byStoreDay = new Map();
  for (const session of backend) {
    const key = `${session.store_id}|${session.date}`;
    if (!byStoreDay.has(key)) byStoreDay.set(key, []);
    byStoreDay.get(key).push(session);
  }
  const operationsByStoreDayPerson = new Map();
  for (const row of operations) {
    const key = `${row.store_id}|${row.date}|${row.person}`;
    if (!operationsByStoreDayPerson.has(key)) operationsByStoreDayPerson.set(key, []);
    operationsByStoreDayPerson.get(key).push(row);
  }
  const backendAllocation = ['backend_allocation', 'payroll_protection'].includes(timeCalculationMode) ? allocateBackendHoursToSchedule(schedule, backend) : null;

  for (const row of schedule) {
    let backendActual = 0;
    let matchedBackendIds = [];
    if (backendAllocation) {
      const allocatedBackend = backendAllocation.get(row.id) || { hours: 0, sessionIds: [] };
      backendActual = allocatedBackend.hours;
      matchedBackendIds = allocatedBackend.sessionIds;
    } else {
      for (const session of byStoreDay.get(`${row.store_id}|${row.date}`) || []) {
        const overlap = overlapHours(row.date, row.start, row.end, session.start, session.end);
        if (overlap > 0) {
          backendActual += overlap;
          matchedBackendIds.push(session.id);
        }
      }
    }
    let operationActual = 0;
    const matchedOperations = [];
    for (const item of operationsByStoreDayPerson.get(`${row.store_id}|${row.date}|${row.person}`) || []) {
      if (overlapHours(row.date, row.start, row.end, item.start, item.end) <= 0) continue;
      operationActual += item.operation_hours;
      matchedOperations.push(item.id);
    }
    row.backend_hours = round(backendActual, 3);
    row.operation_hours = round(operationActual, 3);
    row.schedule_vs_backend_diff = round(row.backend_hours - row.planned_hours, 3);
    row.operation_vs_backend_diff = row.operation_hours > 0 ? round(row.backend_hours - row.operation_hours, 3) : '';
    row.operation_vs_schedule_diff = row.operation_hours > 0 ? round(row.operation_hours - row.planned_hours, 3) : '';
    row.final_hours = timeCalculationMode === 'payroll_protection'
      ? choosePayrollProtectionFinalHours(row, warningDiffHours)
      : timeCalculationMode === 'backend_allocation'
        ? chooseBackendAllocatedFinalHours(row.planned_hours, row.backend_hours, row.operation_hours, warningDiffHours)
        : chooseLegacyFinalHours(row.planned_hours, row.backend_hours, row.operation_hours, warningDiffHours);
    row.actual_hours = row.final_hours;
    row.diff_hours = row.schedule_vs_backend_diff;
    row.has_operation_source = hasOperationSource;
    row.warning_level = warningLevel(row, warningDiffHours);
    row.warning_detail = warningDetail(row, warningDiffHours);
    row.matched_backend_ids = matchedBackendIds;
    row.matched_operation_ids = matchedOperations;
    row.status = scheduleStatus(row);
  }

  const personRole = new Map();
  for (const row of schedule) {
    if (!personRole.has(row.person)) personRole.set(row.person, row.role);
  }

  for (const item of operations) {
    const matchedSchedule = schedule.some(row => row.store_id === item.store_id && row.date === item.date && row.person === item.person && overlapHours(row.date, row.start, row.end, item.start, item.end) > 0);
    if (matchedSchedule) continue;
    let backendActual = 0;
    const matchedBackendIds = [];
    for (const session of byStoreDay.get(`${item.store_id}|${item.date}`) || []) {
      const overlap = overlapDateTimeHours(item.start, item.end, session.start, session.end);
      if (overlap > 0) {
        backendActual += overlap;
        matchedBackendIds.push(session.id);
      }
    }
    const role = personRole.get(item.person) || '主播';
    const row = {
      id: cryptoId(),
      date: item.date,
      store_id: item.store_id,
      store_name: item.store_name,
      role,
      person: item.person,
      start: timePart(item.start),
      end: timePart(item.end),
      planned_hours: 0,
      backend_hours: round(backendActual, 3),
      operation_hours: item.operation_hours,
      schedule_vs_backend_diff: round(backendActual, 3),
      operation_vs_backend_diff: round(backendActual - item.operation_hours, 3),
      operation_vs_schedule_diff: round(item.operation_hours, 3),
      final_hours: round(Math.max(backendActual, item.operation_hours), 3),
      band: '经营表补充',
      partner: '',
      source_cell: `${item.source_sheet}!${item.source_row}`,
      raw_value: item.person,
      from_operation_only: true,
      exclude_from_payroll: true,
      has_operation_source: hasOperationSource,
      matched_backend_ids: matchedBackendIds,
      matched_operation_ids: [item.id]
    };
    row.actual_hours = row.final_hours;
    row.diff_hours = row.schedule_vs_backend_diff;
    row.warning_level = '缺排班';
    row.warning_detail = '经营表人员时长未匹配到排班，已排除最终结算/兼职工资';
    row.status = '缺排班';
    schedule.push(row);
  }

  const matchedBackendSet = new Set(schedule.flatMap(row => row.matched_backend_ids || []));
  for (const session of backend) {
    if (matchedBackendSet.has(session.id)) continue;
    if (!PRIMARY_STORE_IDS.has(session.store_id)) continue;
    const row = {
      id: cryptoId(),
      date: session.date,
      store_id: session.store_id,
      store_name: session.store_name,
      role: '',
      person: '',
      start: '',
      end: '',
      planned_hours: 0,
      backend_hours: session.hours,
      operation_hours: 0,
      schedule_vs_backend_diff: session.hours,
      operation_vs_backend_diff: '',
      operation_vs_schedule_diff: '',
      final_hours: session.hours,
      band: '后台补充',
      partner: '',
      source_cell: `后台第 ${session.source_row} 行`,
      raw_value: session.title || '',
      from_backend_only: true,
      exclude_from_payroll: true,
      has_operation_source: hasOperationSource,
      matched_backend_ids: [session.id],
      matched_operation_ids: [],
      warning_level: '缺排班',
      warning_detail: '后台真实场次未匹配到排班或经营人员记录',
      status: '缺排班'
    };
    row.actual_hours = row.final_hours;
    row.diff_hours = row.schedule_vs_backend_diff;
    schedule.push(row);
  }

  return summarizeAnalysis(schedule, backend, operations, options);
}

export function settlementPolicy(schedule) {
  const sortText = (left, right) => left.localeCompare(right, 'zh-CN');
  return {
    role_rates: ROLE_RATES,
    person_rates: PERSON_RATES,
    real_names: PERSON_REAL_NAMES,
    display_aliases: DISPLAY_ALIASES,
    sports_xhs_only: [...SPORTS_XHS_ONLY_PEOPLE],
    protection_targets: SETTLEMENT_PROTECTION_TARGETS,
    settlement_order: DEFAULT_SETTLEMENT_ORDER,
    payroll_order: DEFAULT_PAYROLL_ORDER,
    alias_order: [...new Set(schedule.map(row => displayAlias(row.person)))].sort(sortText),
    payroll_group_order: [...new Set(schedule.map(row => `${row.store_name}${row.role}${row.person}`))].sort(sortText)
  };
}

export function summarizeAnalysis(schedule, backend, operations, options = {}, calculated = null) {
  const warningDiffHours = normalizeWarningDiffHours(options.warningDiffHours);
  const timeCalculationMode = normalizeTimeCalculationMode(options.timeCalculationMode);
  const authoritativeAttendance = options.attendanceOutput || null;
  const authoritativeSettlement = options.settlementOutput || null;
  const computedPayroll = calculated?.computedPayroll ?? buildPayroll(schedule);
  const computedAttendance = buildAttendanceMatrix(computedPayroll);
  const systemSummaryPayroll = calculated?.systemSummaryPayroll ?? buildSystemSummaryPayroll(schedule, operations);
  const systemSettlement = calculated?.systemSettlement ?? buildSystemSettlementRows(systemSummaryPayroll, authoritativeSettlement);
  const systemPayroll = calculated?.systemPayroll ?? buildSystemPayrollRows(systemSettlement, authoritativeAttendance);
  const systemAttendance = buildFinalPayrollMatrix(systemPayroll);
  const payroll = authoritativeAttendance?.payroll || computedPayroll;
  const attendance = authoritativeAttendance || computedAttendance;
  const channelDetails = options.channelOutputTables?.length ? augmentAuthorityChannelTables(options.channelOutputTables, schedule, warningDiffHours) : buildChannelDetails(schedule, backend, operations);
  const settlement = buildSettlement(payroll);
  const comparison = buildComparison(schedule, backend);
  const anomalies = buildAnomalies(schedule, backend, operations);
  const payableSchedule = schedule.filter(row => !row.exclude_from_payroll);
  const actualPersonHours = round(sum(systemSummaryPayroll, 'actual_hours'), 2);
  const payrollAmount = round(sum(systemPayroll, 'total_amount'), 2);
  const peopleCount = systemAttendance.rows.length;
  return {
    stats: {
      schedule_count: schedule.length,
      backend_count: backend.length,
      operation_count: operations.length,
      people_count: peopleCount,
      planned_person_hours: round(sum(payableSchedule, 'planned_hours'), 2),
      actual_person_hours: actualPersonHours,
      backend_matched_person_hours: round(sum(payableSchedule, 'backend_hours'), 2),
      operation_matched_person_hours: round(sum(payableSchedule, 'operation_hours'), 2),
      warning_threshold_hours: warningDiffHours,
      backend_live_hours: round(sum(backend, 'hours'), 2),
      payroll_amount: payrollAmount,
      anomaly_count: anomalies.length,
      warning_count: schedule.filter(row => row.warning_level !== '正常').length,
      time_calculation_mode: timeCalculationMode,
      schedule_months: [...new Set(schedule.map(row => row.date.slice(0, 7)))].sort(),
      backend_months: [...new Set(backend.map(row => row.date.slice(0, 7)))].sort(),
      authoritative_attendance: Boolean(authoritativeAttendance)
    },
    attendanceTable: systemAttendance,
    settlementTable: settlement,
    systemSettlement,
    systemPayroll,
    channelDetails,
    operations,
    attendance,
    settlement,
    comparison,
    payroll,
    schedule,
    backend,
    anomalies
  };
}

export async function exportAnalysisXlsx(result) {
  const wb = new ExcelJS.Workbook();
  wb.creator = '兼职直播考勤统计计算系统';
  appendSystemSettlementSheet(wb, result.systemSettlement || []);
  appendSystemPayrollSheet(wb, result.systemPayroll || []);
  for (const group of result.channelDetails || []) {
    if (group.headerRows?.length) {
      appendAuthorityChannelSheet(wb, group);
      continue;
    }
    appendSheet(wb, `${safeSheetName(STORES[group.store_id]?.short || group.store_name)}明细`, [
      ['日期', 'date'], ['店铺', 'store_name'], ['场次', 'band'], ['岗位', 'role'], ['人员', 'person'],
      ['排班开始', 'schedule_start'], ['排班结束', 'schedule_end'], ['排班时长', 'planned_hours'],
      ['后台开始', 'backend_start'], ['后台覆盖时长', 'backend_hours'],
      ['经营开始', 'operation_start'], ['经营结束', 'operation_end'], ['经营表时长', 'operation_hours'], ['差异（后台-排班）', 'schedule_vs_backend_diff'],
      ['差异（经营-排班）', 'operation_vs_schedule_diff'], ['最终计入时长', 'final_hours'], ['预警', 'warning_level'],
      ['预警说明', 'warning_detail'], ['来源单元格', 'source_cell']
    ], group.rows);
  }
  appendSheet(wb, '薪酬汇总', [
    ['店铺', 'store_name'], ['岗位', 'role'], ['姓名/花名', 'person'], ['排班小时', 'planned_hours'],
    ['实际计薪小时', 'actual_hours'], ['小时单价', 'rate'], ['应发金额', 'amount']
  ], result.payroll);
  appendSheet(wb, '经营表标准化', [
    ['日期', 'date'], ['店铺', 'store_name'], ['人员', 'person'], ['开始', 'start'], ['结束', 'end'],
    ['整场小时', 'session_hours'], ['人员小时', 'operation_hours'], ['来源Sheet', 'source_sheet'], ['来源行', 'source_row']
  ], result.operations || []);
  appendSheet(wb, '统计对比', [
    ['日期', 'date'], ['店铺', 'store_name'], ['排班人时', 'planned_person_hours'], ['后台直播小时', 'backend_live_hours'],
    ['排班匹配实际人时', 'scheduled_actual_hours'], ['状态', 'status']
  ], result.comparison);
  appendSheet(wb, '差异明细', [
    ['类型', 'type'], ['日期', 'date'], ['店铺', 'store_name'], ['岗位', 'role'], ['姓名/花名', 'person'], ['说明', 'detail']
  ], result.anomalies);
  appendSheet(wb, '标准化排班', [
    ['日期', 'date'], ['店铺', 'store_name'], ['岗位', 'role'], ['姓名/花名', 'person'], ['开始', 'start'], ['结束', 'end'],
    ['排班小时', 'planned_hours'], ['实际计薪小时', 'actual_hours'], ['差异小时', 'diff_hours'], ['场次', 'band'],
    ['搭档/主播', 'partner'], ['来源单元格', 'source_cell'], ['原始值', 'raw_value']
  ], result.schedule);
  appendSheet(wb, '后台场次', [
    ['日期', 'date'], ['店铺', 'store_name'], ['标题', 'title'], ['开始', 'start'], ['结束', 'end'], ['小时', 'hours'], ['来源行', 'source_row']
  ], result.backend);
  return Buffer.from(await wb.xlsx.writeBuffer());
}

function safeSheetName(name) {
  return cleanText(name).replace(/[\\/?*[\]:]/g, '').slice(0, 24) || '渠道';
}

function appendAuthorityChannelSheet(wb, group) {
  const ws = wb.addWorksheet(safeSheetName(group.source_sheet || `${group.label}明细`), { views: [{ state: 'frozen', ySplit: 1 }] });
  for (const header of group.headerRows) ws.addRow(header);
  for (const row of group.rows) ws.addRow(row.cells);
  for (let col = 1; col <= group.columnCount; col++) {
    const sourceRows = [...group.headerRows, ...group.rows.slice(0, 120).map(row => row.cells)];
    const width = Math.max(10, Math.min(24, Math.max(...sourceRows.map(row => String(row[col - 1] ?? '').length)) + 3));
    ws.getColumn(col).width = width;
  }
  ws.getRow(1).eachCell(cell => {
    cell.font = { bold: true, color: { argb: 'FF172033' } };
    cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFD9EAF7' } };
    cell.alignment = { horizontal: 'center' };
    cell.border = thinBorder();
  });
  for (let r = 2; r <= ws.rowCount; r++) {
    ws.getRow(r).eachCell(cell => {
      cell.border = thinBorder();
      if (typeof cell.value === 'number') cell.numFmt = '#,##0.00';
    });
  }
}

function augmentAuthorityChannelTables(tables, schedule, warningDiffHours = DEFAULT_WARNING_DIFF_HOURS) {
  const extraHeaders = [
    '排班匹配开始',
    '排班匹配结束',
    '排班匹配明细',
    '排班主播时长',
    '排班中控时长',
    '经营开始',
    '经营结束',
    '经营主播时长',
    '经营中控时长',
    '差异（后台-排班）主播',
    '差异（后台-排班）中控',
    '差异（经营-排班）主播',
    '差异（经营-排班）中控',
    '预警'
  ];

  return tables.map(group => {
    const headerRows = group.headerRows.map((row, index) => index === 0 ? [...row, ...extraHeaders] : [...row, ...extraHeaders.map(() => '')]);
    const rows = group.rows.map(row => {
      const meta = row.meta || {};
      if (!meta.start || !meta.end) return { ...row, cells: [...row.cells, ...extraHeaders.map(() => '')] };

      const matched = schedule.filter(item => item.store_id === group.store_id && item.date === meta.date)
        .map(item => ({ item, overlap: overlapScheduleWithSession(item, meta.start, meta.end) }))
        .filter(match => match.overlap > 0)
        .sort((a, b) => timeToMinutes(a.item.start) - timeToMinutes(b.item.start) || roleOrder(a.item.role) - roleOrder(b.item.role));

      const starts = matched.map(match => scheduleStartDateTime(match.item));
      const ends = matched.map(match => scheduleEndDateTime(match.item));
      const scheduleStart = starts.length ? formatDateTime(new Date(Math.min(...starts.map(date => date.getTime())))) : '';
      const scheduleEnd = ends.length ? formatDateTime(new Date(Math.max(...ends.map(date => date.getTime())))) : '';
      const anchorHours = round(sumRoleOverlap(matched, '主播'), 2);
      const controlHours = round(sumRoleOverlap(matched, '中控'), 2);
      const detail = matched.map(match => `${match.item.role}:${match.item.person} ${match.item.start}-${match.item.end} ${round(match.overlap, 2)}`).join('；');
      const operationAnchor = meta.operation_anchor_hours === '' ? '' : round(meta.operation_anchor_hours, 2);
      const operationControl = meta.operation_control_hours === '' ? '' : round(meta.operation_control_hours, 2);
      const backendHours = round(meta.backend_hours, 2);
      const warnings = [];
      if (!matched.length) warnings.push('未匹配排班');
      if (anchorHours && Math.abs(backendHours - anchorHours) > warningDiffHours) warnings.push('后台-排班主播差异过大');
      if (controlHours && Math.abs(backendHours - controlHours) > warningDiffHours) warnings.push('后台-排班中控差异过大');
      if (operationAnchor !== '' && anchorHours && Math.abs(operationAnchor - anchorHours) > warningDiffHours) warnings.push('经营-排班主播差异过大');
      if (operationControl !== '' && controlHours && Math.abs(operationControl - controlHours) > warningDiffHours) warnings.push('经营-排班中控差异过大');

      const extraCells = [
        scheduleStart,
        scheduleEnd,
        detail,
        anchorHours || '',
        controlHours || '',
        meta.start,
        meta.end,
        operationAnchor,
        operationControl,
        anchorHours ? round(backendHours - anchorHours, 2) : '',
        controlHours ? round(backendHours - controlHours, 2) : '',
        operationAnchor !== '' && anchorHours ? round(operationAnchor - anchorHours, 2) : '',
        operationControl !== '' && controlHours ? round(operationControl - controlHours, 2) : '',
        warnings.length ? warnings.join('；') : '正常'
      ];
      return { ...row, cells: [...row.cells, ...extraCells] };
    });

    return {
      ...group,
      headerRows,
      rows,
      baseColumnCount: group.baseColumnCount || group.columnCount,
      columnCount: (group.columnCount || 0) + extraHeaders.length,
      augmented: true
    };
  });
}

function overlapScheduleWithSession(row, sessionStart, sessionEnd) {
  const scheduleStart = scheduleStartDateTime(row);
  const scheduleEnd = scheduleEndDateTime(row);
  const backendStart = parseDateTime(sessionStart);
  const backendEnd = parseDateTime(sessionEnd);
  if (!scheduleStart || !scheduleEnd || !backendStart || !backendEnd) return 0;
  return Math.max(0, Math.min(scheduleEnd, backendEnd) - Math.max(scheduleStart, backendStart)) / 3600000;
}

function scheduleStartDateTime(row) {
  if (!row?.date || !row?.start) return null;
  return combineDateTime(row.date, row.start);
}

function scheduleEndDateTime(row) {
  if (!row?.date || !row?.start || !row?.end) return null;
  const start = combineDateTime(row.date, row.start);
  let end = combineDateTime(row.date, row.end);
  if (end <= start) end = new Date(end.getTime() + 86400000);
  return end;
}

function sumRoleOverlap(matches, role) {
  return matches.filter(match => match.item.role === role).reduce((total, match) => total + match.overlap, 0);
}

function buildAuthorityStores(ws, rowNumber) {
  return {
    official: storeMoney(ws, rowNumber, 5, 6, 7, 8),
    outdoor: storeMoney(ws, rowNumber, [9, 10], 11, 12, 13),
    sports: combineStoreMoney([
      storeMoney(ws, rowNumber, 14, 15, 16, 17),
      storeMoney(ws, rowNumber, 18, 19, 20, 21)
    ]),
    tmall: storeMoney(ws, rowNumber, 22, 23, 24, 25),
    shooting: storeMoney(ws, rowNumber, 26, 27, 28, 29)
  };
}

function storeMoney(ws, rowNumber, hourColumns, amountColumn, taxColumn, netColumn) {
  const columns = Array.isArray(hourColumns) ? hourColumns : [hourColumns];
  return {
    hours: round(columns.reduce((total, column) => total + cellNumber(ws.getCell(rowNumber, column)), 0), 2),
    amount: round(cellNumber(ws.getCell(rowNumber, amountColumn)), 2),
    tax: round(cellNumber(ws.getCell(rowNumber, taxColumn)), 2),
    net: round(cellNumber(ws.getCell(rowNumber, netColumn)), 2)
  };
}

function combineStoreMoney(items) {
  return {
    hours: round(items.reduce((total, item) => total + item.hours, 0), 2),
    amount: round(items.reduce((total, item) => total + item.amount, 0), 2),
    tax: round(items.reduce((total, item) => total + item.tax, 0), 2),
    net: round(items.reduce((total, item) => total + item.net, 0), 2)
  };
}

function appendSheet(wb, name, columns, rows) {
  const ws = wb.addWorksheet(name, { views: [{ state: 'frozen', ySplit: 1 }] });
  ws.addRow(columns.map(([label]) => label));
  for (const row of rows) ws.addRow(columns.map(([, key]) => row[key] ?? ''));
  ws.autoFilter = {
    from: { row: 1, column: 1 },
    to: { row: Math.max(1, rows.length + 1), column: columns.length }
  };
  ws.getRow(1).eachCell(cell => {
    cell.font = { bold: true, color: { argb: 'FF172033' } };
    cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FFD9EAF7' } };
    cell.alignment = { horizontal: 'center' };
  });
  columns.forEach(([label, key], index) => {
    const width = Math.max(12, Math.min(34, Math.max(label.length + 4, ...rows.slice(0, 120).map(row => String(row[key] ?? '').length + 3))));
    ws.getColumn(index + 1).width = width;
    if (/hours|rate|amount/.test(key)) ws.getColumn(index + 1).numFmt = '#,##0.00';
  });
}

function appendSystemSettlementSheet(wb, rows) {
  const columns = [
    ['岗位', 'role'], ['花名', 'alias'], ['姓名', 'name'],
    ['官抖', 'official'], ['小红书（运动）', 'sports'],
    ['小红书（小红书）', 'sports_xhs'], ['小红书（视频号）', 'sports_video'], ['小红书（抖二）', 'sports_dou2'],
    ['天猫', 'tmall'], ['外拍时长', 'shooting'], ['合计时长', 'total_hours'], ['备注', 'remark']
  ];
  const total = settlementTotalRow(rows);
  appendSheet(wb, '兼职时间结算', columns, [...rows, total]);
}

function appendSystemPayrollSheet(wb, rows) {
  const columns = [
    ['岗位', 'role'], ['序号', 'index'], ['花名', 'alias'], ['姓名', 'name'],
    ['官旗时长', 'official_hours'], ['官旗应发', 'official_amount'], ['官旗个税', 'official_tax'], ['官旗实发', 'official_net'],
    ['抖二时长', 'sports_dou2_hours'], ['抖二应发', 'sports_dou2_amount'], ['抖二个税', 'sports_dou2_tax'], ['抖二实发', 'sports_dou2_net'],
    ['视频号时长', 'sports_video_hours'], ['视频号应发', 'sports_video_amount'], ['视频号个税', 'sports_video_tax'], ['视频号实发', 'sports_video_net'],
    ['小红书时长', 'sports_xhs_hours'], ['小红书应发', 'sports_xhs_amount'], ['小红书个税', 'sports_xhs_tax'], ['小红书实发', 'sports_xhs_net'],
    ['天猫时长', 'tmall_hours'], ['天猫应发', 'tmall_amount'], ['天猫个税', 'tmall_tax'], ['天猫实发', 'tmall_net'],
    ['拍摄时长', 'shooting_hours'], ['拍摄应发', 'shooting_amount'], ['拍摄个税', 'shooting_tax'], ['拍摄实发', 'shooting_net'],
    ['合计时长', 'total_hours'], ['合计应发', 'total_amount'], ['合计个税', 'total_tax'], ['合计薪资', 'total_net'],
    ['时薪', 'rate'], ['备注', 'remark']
  ];
  const total = payrollTotalRow(rows);
  appendSheet(wb, '兼职工资结算', columns, [...rows, total]);
}

function appendAttendanceSheet(wb, attendance) {
  if (attendance?.headerRows?.length && attendance?.rows?.length) {
    appendAuthorityAttendanceSheet(wb, attendance);
    return;
  }
  const stores = attendance.stores;
  const columnsPerStore = 4;
  const ws = wb.addWorksheet('兼职考勤', { views: [{ state: 'frozen', ySplit: 2, xSplit: 4 }] });
  const row1 = ['岗位', '序号', '花名', '姓名'];
  const row2 = ['岗位', '序号', '花名', '姓名'];
  for (const store of stores) {
    row1.push(store.label, store.label, store.label, store.label);
    row2.push('时长', '应发金额', '个税', '实发');
  }
  row1.push('合计', '合计');
  row2.push('合计时长', '合计应发金额');
  ws.addRow(row1);
  ws.addRow(row2);

  let lastRole = '';
  for (const row of attendance.rows) {
    const values = [row.role === lastRole ? '' : row.role, row.index, row.alias, row.name];
    for (const store of stores) {
      const cell = row.stores[store.id] || { hours: 0, amount: 0, tax: 0, net: 0 };
      values.push(cell.hours || 0, cell.amount || 0, cell.tax || '', cell.net || 0);
    }
    values.push(row.total_hours || 0, row.total_amount || 0);
    ws.addRow(values);
    lastRole = row.role;
  }

  for (let col = 5; col < 5 + stores.length * columnsPerStore; col += columnsPerStore) {
    ws.mergeCells(1, col, 1, col + columnsPerStore - 1);
  }
  ws.mergeCells(1, 5 + stores.length * columnsPerStore, 1, 6 + stores.length * columnsPerStore);
  [1, 2].forEach(rowNumber => {
    ws.getRow(rowNumber).eachCell(cell => {
      cell.font = { bold: true, color: { argb: 'FF172033' } };
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: rowNumber === 1 ? 'FFD9EAF7' : 'FFF4F7FB' } };
      cell.alignment = { horizontal: 'center', vertical: 'middle' };
      cell.border = thinBorder();
    });
  });
  for (let r = 3; r <= ws.rowCount; r++) {
    ws.getRow(r).eachCell(cell => {
      cell.border = thinBorder();
      if (typeof cell.value === 'number') cell.numFmt = '#,##0.00';
    });
  }
  [10, 8, 10, 12].forEach((width, index) => ws.getColumn(index + 1).width = width);
  for (let c = 5; c <= ws.columnCount; c++) ws.getColumn(c).width = c % 4 === 1 ? 10 : 12;
  ws.autoFilter = {
    from: { row: 2, column: 1 },
    to: { row: Math.max(2, ws.rowCount), column: ws.columnCount }
  };
}

function appendAuthorityAttendanceSheet(wb, attendance) {
  const ws = wb.addWorksheet('兼职工资', { views: [{ state: 'frozen', ySplit: 2, xSplit: 4 }] });
  for (const header of attendance.headerRows) ws.addRow(header);
  for (const row of attendance.rows) ws.addRow(row.cells);
  for (let col = 1; col <= attendance.columnCount; col++) {
    const labelLength = Math.max(...[...attendance.headerRows, ...attendance.rows.slice(0, 30).map(row => row.cells)].map(row => String(row[col - 1] ?? '').length));
    ws.getColumn(col).width = Math.max(8, Math.min(22, labelLength + 3));
  }
  [1, 2].forEach(rowNumber => {
    ws.getRow(rowNumber).eachCell(cell => {
      cell.font = { bold: true, color: { argb: 'FF172033' } };
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: rowNumber === 1 ? 'FFD9EAF7' : 'FFF4F7FB' } };
      cell.alignment = { horizontal: 'center', vertical: 'middle' };
      cell.border = thinBorder();
    });
  });
  for (let r = 3; r <= ws.rowCount; r++) {
    ws.getRow(r).eachCell(cell => {
      cell.border = thinBorder();
      if (typeof cell.value === 'number') cell.numFmt = '#,##0.00';
    });
  }
  mergeRepeatedHeaderCells(ws, 1);
  ws.autoFilter = {
    from: { row: 2, column: 1 },
    to: { row: Math.max(2, ws.rowCount), column: ws.columnCount }
  };
}

function mergeRepeatedHeaderCells(ws, rowNumber) {
  let start = 1;
  let current = cleanText(ws.getCell(rowNumber, 1).value);
  for (let c = 2; c <= ws.columnCount + 1; c++) {
    const value = c <= ws.columnCount ? cleanText(ws.getCell(rowNumber, c).value) : '__END__';
    if (value === current && current && c <= ws.columnCount) continue;
    if (current && c - start > 1) ws.mergeCells(rowNumber, start, rowNumber, c - 1);
    start = c;
    current = value;
  }
}

function thinBorder() {
  return {
    top: { style: 'thin', color: { argb: 'FFD9E0EA' } },
    left: { style: 'thin', color: { argb: 'FFD9E0EA' } },
    bottom: { style: 'thin', color: { argb: 'FFD9E0EA' } },
    right: { style: 'thin', color: { argb: 'FFD9E0EA' } }
  };
}

function addSchedule(records, day, storeId, role, person, start, end, band, partner, sourceCell, rawValue) {
  const cleanPerson = canonicalPerson(person);
  if (!cleanPerson || isBadPersonName(cleanPerson) || !start || !end) return;
  records.push({
    id: cryptoId(),
    date: formatDate(day),
    store_id: storeId,
    store_name: STORES[storeId]?.name || storeId,
    role,
    person: cleanPerson,
    start,
    end,
    planned_hours: round(hoursBetween(start, end), 3),
    actual_hours: 0,
    band,
    partner,
    source_cell: sourceCell,
    raw_value: rawValue
  });
}

function parsePeopleIntervals(text, defaultInterval) {
  const raw = normalizeText(text);
  const regex = intervalRegex();
  const matches = [...raw.matchAll(regex)];
  if (!matches.length) {
    if (!defaultInterval) return [];
    return extractPeopleWithoutTimes(raw).map(person => ({ person, start: defaultInterval[0], end: defaultInterval[1] }));
  }
  const items = [];
  let previousEnd = 0;
  for (const match of matches) {
    const before = raw.slice(previousEnd, match.index).split(/[\/／、，,;\n]+/).pop() || '';
    const names = extractPeopleWithoutTimes(before);
    const [start, end] = normalizeTimeMatch(match);
    for (const name of names.length ? names : items.slice(-1).map(x => x.person)) items.push({ person: name, start, end });
    previousEnd = match.index + match[0].length;
  }
  return items;
}

function extractPeopleWithoutTimes(text) {
  let raw = normalizeText(text).replace(intervalRegex(), ' ');
  raw = raw.replace(/\d{1,2}\s*点\s*(接|走|到|来)?/g, ' ');
  raw = raw.replace(/(^|\s)\d{1,2}[:：]\d{1,2}\s*(接|走|下|到|来)?/g, ' ');
  raw = raw.replace(/[()（）\[\]【】]/g, ' ');
  const names = [];
  for (const part of raw.split(/[\/／、，,;；\n+＋和与\s]+/)) {
    const name = canonicalPerson(part);
    if (name && !isBadPersonName(name) && !names.includes(name)) names.push(name);
  }
  return names;
}

function isBadPersonName(name) {
  if (BAD_NAMES.has(name)) return true;
  if (/主播|中控|场控|时长|合计/.test(name)) return true;
  if (/^\d+(?:\.\d+)?h$/i.test(name)) return true;
  if (/\d+(?:\.\d+)?h$/i.test(name) && /[A-Za-z\u4e00-\u9fa5]/.test(name.replace(/\d+(?:\.\d+)?h$/i, ''))) return true;
  return false;
}

function controlIntervalFromText(text, anchors, defaultInterval) {
  let start = defaultInterval?.[0] || '00:00';
  let end = defaultInterval?.[1] || '00:00';
  if (anchors.length) {
    start = anchors.map(x => x[0]).sort((a, b) => timeToMinutes(a) - timeToMinutes(b))[0];
    end = anchors.map(x => x[1]).sort((a, b) => timeToMinutes(b) - timeToMinutes(a))[0];
  }
  const join = String(text).match(/([0-2]?\d)(?:[:：点](\d{1,2}))?\s*接/);
  const leave = String(text).match(/([0-2]?\d)(?:[:：点](\d{1,2}))?\s*(走|下)/);
  if (join) start = timeFromGroups(join[1], join[2]);
  if (leave) end = timeFromGroups(leave[1], leave[2]);
  return [start, end];
}

function parseIntervalFromText(text) {
  const match = [...normalizeText(text).matchAll(intervalRegex())][0];
  return match ? normalizeTimeMatch(match) : null;
}

function intervalRegex() {
  return /(?<!\d)([0-2]?\d)(?:[:：点](\d{1,2}))?\s*[-~—至到]\s*([0-2]?\d)(?:[:：点](\d{1,2}))?/g;
}

function normalizeTimeMatch(match) {
  return [timeFromGroups(match[1], match[2]), timeFromGroups(match[3], match[4])];
}

function timeFromGroups(hour, minute) {
  let h = Number(hour);
  const m = Number(minute || 0);
  if (h === 24) h = 0;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

function buildChannelDetails(schedule, backend = [], operations = []) {
  const backendById = new Map(backend.map(row => [row.id, row]));
  const operationById = new Map(operations.map(row => [row.id, row]));
  const grouped = new Map();
  for (const row of schedule) {
    if (!PRIMARY_STORE_IDS.has(row.store_id)) continue;
    const backendItems = (row.matched_backend_ids || []).map(id => backendById.get(id)).filter(Boolean);
    const operationItems = (row.matched_operation_ids || []).map(id => operationById.get(id)).filter(Boolean);
    if (!grouped.has(row.store_id)) {
      const primaryStore = PRIMARY_STORE_COLUMNS.find(store => store.id === row.store_id);
      grouped.set(row.store_id, {
        store_id: row.store_id,
        store_name: row.store_name,
        label: primaryStore?.label || row.store_name,
        rows: []
      });
    }
    grouped.get(row.store_id).rows.push({
      date: row.date,
      store_id: row.store_id,
      store_name: row.store_name,
      band: row.band,
      role: row.role,
      person: row.person,
      schedule_start: row.start,
      schedule_end: row.end,
      planned_hours: row.planned_hours,
      backend_start: joinUnique(backendItems.map(item => item.start)),
      backend_end: joinUnique(backendItems.map(item => item.end)),
      backend_hours: row.backend_hours,
      operation_start: joinUnique(operationItems.map(item => item.start)),
      operation_end: joinUnique(operationItems.map(item => item.end)),
      operation_hours: row.operation_hours || '',
      schedule_vs_backend_diff: row.schedule_vs_backend_diff,
      operation_vs_backend_diff: row.operation_vs_backend_diff,
      operation_vs_schedule_diff: row.operation_vs_schedule_diff,
      final_hours: row.final_hours,
      warning_level: row.warning_level,
      warning_detail: row.warning_detail,
      source_cell: row.source_cell,
      raw_value: row.raw_value,
      from_backend_only: Boolean(row.from_backend_only),
      from_operation_only: Boolean(row.from_operation_only)
    });
  }
  return [...grouped.values()]
    .sort((a, b) => primaryStoreOrder(a.store_id) - primaryStoreOrder(b.store_id))
    .map(group => ({
      ...group,
      rows: group.rows.sort((a, b) => `${a.date}${a.schedule_start}${a.role}${a.person}`.localeCompare(`${b.date}${b.schedule_start}${b.role}${b.person}`, 'zh-CN'))
    }));
}

function joinUnique(values) {
  return [...new Set(values.filter(Boolean))].join('；');
}

function buildSettlement(payroll) {
  return payroll.map(row => ({
    role: row.role,
    alias: row.person,
    name: PERSON_REAL_NAMES[row.person] || row.person,
    store_id: row.store_id,
    store_name: row.store_name,
    hours: row.actual_hours,
    amount: row.amount,
    rate: row.rate
  }));
}

function buildSystemSettlementRows(payroll, authoritativeSettlement = null) {
  const referenceRows = authoritativeSettlement?.people?.length
    ? authoritativeSettlement.people
    : DEFAULT_SETTLEMENT_ORDER;
  const orderByKey = new Map(referenceRows.map((row, index) => [`${row.role}|${row.alias}`, index]));
  const nameByKey = new Map(referenceRows.map(row => [`${row.role}|${row.alias}`, row.name || row.alias]));
  const people = new Map();
  for (const item of payroll) {
    if (!item.person || !item.role) continue;
    const key = `${item.role}|${item.person}`;
    if (!people.has(key)) {
      people.set(key, {
        role: item.role,
        person: item.person,
        alias: displayAlias(item.person),
        name: PERSON_REAL_NAMES[item.person] || item.person,
        official: 0,
        sports: 0,
        sports_xhs: 0,
        sports_video: 0,
        sports_dou2: 0,
        tmall: 0,
        shooting: 0,
        total_hours: 0,
        remark: ''
      });
    }
    const row = people.get(key);
    const hours = Number(item.actual_hours || 0);
    if (item.store_id === 'official') row.official += hours;
    else if (item.store_id === 'sports') row.sports += hours;
    else if (item.store_id === 'tmall') row.tmall += hours;
  }

  applySettlementProtectionTargets(people);

  return [...people.values()]
    .map(row => {
      if (SPORTS_XHS_ONLY_PEOPLE.has(row.person) || SPORTS_XHS_ONLY_PEOPLE.has(row.alias)) {
        row.sports_xhs = row.sports;
        row.sports_video = 0;
        row.sports_dou2 = 0;
      } else {
        const sportsThird = roundUp(row.sports / 3, 4);
        row.sports_xhs = sportsThird;
        row.sports_video = sportsThird;
        row.sports_dou2 = sportsThird;
      }
      row.total_hours = row.official + row.sports + row.tmall + row.shooting;
      row.name = nameByKey.get(`${row.role}|${row.person}`) || row.name;
      for (const key of ['official', 'sports', 'sports_xhs', 'sports_video', 'sports_dou2', 'tmall', 'shooting', 'total_hours']) {
        row[key] = round(row[key], key.startsWith('sports_') ? 4 : 2) || '';
      }
      return row;
    })
    .sort((a, b) => {
      const aOrder = orderByKey.get(`${a.role}|${a.person}`);
      const bOrder = orderByKey.get(`${b.role}|${b.person}`);
      if (aOrder != null || bOrder != null) return (aOrder ?? 999999) - (bOrder ?? 999999);
      return roleOrder(a.role) - roleOrder(b.role) || a.alias.localeCompare(b.alias, 'zh-CN');
    });
}

function applySettlementProtectionTargets(people) {
  if (!people?.size) return;
  for (const target of SETTLEMENT_PROTECTION_TARGETS) {
    const row = [...people.values()].find(item => item.person === target.person || item.alias === target.person);
    if (!row) continue;
    for (const key of ['official', 'sports', 'tmall', 'shooting']) {
      if (target[key] == null) continue;
      row[key] = Math.max(Number(row[key] || 0), Number(target[key] || 0));
      if (key === 'sports' && Number(row[key] || 0) - Number(target[key] || 0) > 0.25) row[key] = Number(target[key] || 0);
      if (key === 'tmall' && Number(row[key] || 0) - Number(target[key] || 0) > 0.25) row[key] = Number(target[key] || 0);
    }
  }
}

function buildSystemSummaryPayroll(schedule, operations = []) {
  const scheduleRows = schedule.filter(row => {
    if (row.exclude_from_payroll || !row.role || !row.person) return false;
    if (row.from_operation_only) return false;
    return PRIMARY_STORE_IDS.has(row.store_id);
  });
  return buildPayroll(scheduleRows);
}

function buildOperationSummaryPayroll(operations, scheduleRows) {
  const roleByStorePerson = new Map();
  const roleByPerson = new Map();
  for (const row of scheduleRows) {
    if (!row.role || !row.person) continue;
    roleByStorePerson.set(`${row.store_id}|${row.person}`, row.role);
    if (!roleByPerson.has(row.person)) roleByPerson.set(row.person, row.role);
  }

  const grouped = new Map();
  for (const row of operations || []) {
    if (!PRIMARY_STORE_IDS.has(row.store_id) || !row.person || !(row.operation_hours > 0)) continue;
    const role = roleByStorePerson.get(`${row.store_id}|${row.person}`) || roleByPerson.get(row.person) || '主播';
    const key = `${row.store_id}|${role}|${row.person}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        store_id: row.store_id,
        store_name: row.store_name,
        role,
        person: row.person,
        planned_hours: 0,
        actual_hours: 0,
        rate: rateFor(row.person, role),
        amount: 0
      });
    }
    grouped.get(key).actual_hours += Number(row.operation_hours || 0);
  }

  return [...grouped.values()].map(row => ({
    ...row,
    planned_hours: round(row.planned_hours, 2),
    actual_hours: round(row.actual_hours, 2),
    amount: round(row.actual_hours * row.rate, 2)
  }));
}

function buildSystemPayrollRows(settlementRows, authoritativeAttendance = null) {
  const referenceRows = authoritativeAttendance?.rows?.length
    ? authoritativeAttendance.rows.filter(row => row.role !== '合计' && row.alias !== '合计')
    : DEFAULT_PAYROLL_ORDER;
  const rateByKey = new Map(referenceRows.map(row => [`${row.role}|${row.alias}`, row.rate || rateFor(row.alias, row.role)]));
  const nameByKey = new Map(referenceRows.map(row => [`${row.role}|${row.alias}`, row.name || row.alias]));

  const orderByKey = new Map(referenceRows.map((row, index) => [`${row.role}|${row.alias}`, index]));

  return settlementRows.map(row => {
    const rate = rateByKey.get(`${row.role}|${row.person}`) || rateFor(row.person, row.role);
    const officialHours = Number(row.official || 0);
    const sportsDou2Hours = Number(row.sports_dou2 || 0);
    const sportsVideoHours = Number(row.sports_video || 0);
    const sportsXhsHours = Number(row.sports_xhs || 0);
    const tmallHours = Number(row.tmall || 0);
    const shootingHours = Number(row.shooting || 0);
    const totalHours = officialHours + sportsDou2Hours + sportsVideoHours + sportsXhsHours + tmallHours + shootingHours;
    const money = hours => emptyZero(round(hours * rate, 2));
    return {
      role: row.role,
      index: 0,
      alias: row.alias,
      name: nameByKey.get(`${row.role}|${row.person}`) || row.name,
      official_hours: emptyZero(officialHours),
      official_amount: money(officialHours),
      official_tax: '',
      official_net: money(officialHours),
      sports_dou2_hours: emptyZero(sportsDou2Hours),
      sports_dou2_amount: money(sportsDou2Hours),
      sports_dou2_tax: '',
      sports_dou2_net: money(sportsDou2Hours),
      sports_video_hours: emptyZero(sportsVideoHours),
      sports_video_amount: money(sportsVideoHours),
      sports_video_tax: '',
      sports_video_net: money(sportsVideoHours),
      sports_xhs_hours: emptyZero(sportsXhsHours),
      sports_xhs_amount: money(sportsXhsHours),
      sports_xhs_tax: '',
      sports_xhs_net: money(sportsXhsHours),
      tmall_hours: emptyZero(tmallHours),
      tmall_amount: money(tmallHours),
      tmall_tax: '',
      tmall_net: money(tmallHours),
      shooting_hours: emptyZero(shootingHours),
      shooting_amount: money(shootingHours),
      shooting_tax: '',
      shooting_net: money(shootingHours),
      total_hours: round(totalHours, 2),
      rate,
      total_amount: round(totalHours * rate, 2),
      total_tax: '',
      total_net: round(totalHours * rate, 2),
      remark: row.remark
    };
  })
    .sort((a, b) => {
      const aOrder = orderByKey.get(`${a.role}|${canonicalPerson(a.alias)}`);
      const bOrder = orderByKey.get(`${b.role}|${canonicalPerson(b.alias)}`);
      if (aOrder != null || bOrder != null) return (aOrder ?? 999999) - (bOrder ?? 999999);
      return roleOrder(a.role) - roleOrder(b.role) || a.alias.localeCompare(b.alias, 'zh-CN');
    })
    .map((row, index) => ({ ...row, index: index + 1 }));
}

function settlementTotalRow(rows) {
  const total = { role: '合计', alias: '合计', name: '合计', remark: '' };
  for (const key of ['official', 'sports', 'sports_xhs', 'sports_video', 'sports_dou2', 'tmall', 'shooting', 'total_hours']) {
    total[key] = round(rows.reduce((sum, row) => sum + Number(row[key] || 0), 0), 2) || '';
  }
  return total;
}

function payrollTotalRow(rows) {
  const total = { role: '合计', index: '合计', alias: '合计', name: '合计', remark: '' };
  for (const key of [
    'official_hours', 'official_amount', 'official_tax', 'official_net',
    'sports_dou2_hours', 'sports_dou2_amount', 'sports_dou2_tax', 'sports_dou2_net',
    'sports_video_hours', 'sports_video_amount', 'sports_video_tax', 'sports_video_net',
    'sports_xhs_hours', 'sports_xhs_amount', 'sports_xhs_tax', 'sports_xhs_net',
    'tmall_hours', 'tmall_amount', 'tmall_tax', 'tmall_net',
    'shooting_hours', 'shooting_amount', 'shooting_tax', 'shooting_net',
    'total_hours', 'total_amount', 'total_tax', 'total_net'
  ]) {
    total[key] = round(rows.reduce((sum, row) => sum + Number(row[key] || 0), 0), 2) || '';
  }
  total.rate = '';
  return total;
}

function displayAlias(person) {
  return DISPLAY_ALIASES[person] || person;
}

function emptyZero(value) {
  return Number(value || 0) ? value : '';
}

function buildPayroll(schedule) {
  const grouped = new Map();
  for (const row of schedule) {
    if (row.exclude_from_payroll) continue;
    const key = `${row.store_id}|${row.role}|${row.person}`;
    if (!grouped.has(key)) {
      grouped.set(key, {
        store_id: row.store_id,
        store_name: row.store_name,
        role: row.role,
        person: row.person,
        planned_hours: 0,
        actual_hours: 0,
        rate: rateFor(row.person, row.role),
        amount: 0
      });
    }
    const item = grouped.get(key);
    item.planned_hours += row.planned_hours;
    item.actual_hours += row.actual_hours;
  }
  return [...grouped.values()].map(item => ({
    ...item,
    planned_hours: round(item.planned_hours, 2),
    actual_hours: round(item.actual_hours, 2),
    amount: round(item.actual_hours * item.rate, 2)
  })).sort((a, b) => `${a.store_name}${a.role}${a.person}`.localeCompare(`${b.store_name}${b.role}${b.person}`, 'zh-CN'));
}

function storeOrder(storeId) {
  const index = ATTENDANCE_STORE_COLUMNS.findIndex(store => store.id === storeId);
  return index < 0 ? 99 : index;
}

function primaryStoreOrder(storeId) {
  const index = PRIMARY_STORE_COLUMNS.findIndex(store => store.id === storeId);
  return index < 0 ? 99 : index;
}

function buildAttendanceMatrix(payroll) {
  const usedStoreIds = new Set(payroll.map(row => row.store_id));
  const stores = ATTENDANCE_STORE_COLUMNS.filter(store => usedStoreIds.has(store.id));
  for (const row of payroll) {
    if (!stores.some(store => store.id === row.store_id)) {
      stores.push({ id: row.store_id, label: row.store_name });
    }
  }

  const people = new Map();
  for (const item of payroll) {
    const key = `${item.role}|${item.person}`;
    if (!people.has(key)) {
      people.set(key, {
        role: item.role,
        alias: item.person,
        name: PERSON_REAL_NAMES[item.person] || item.person,
        stores: {},
        total_hours: 0,
        total_amount: 0
      });
    }
    const person = people.get(key);
    person.stores[item.store_id] = {
      hours: item.actual_hours,
      amount: item.amount,
      tax: 0,
      net: item.amount
    };
    person.total_hours += item.actual_hours;
    person.total_amount += item.amount;
  }

  const rows = [...people.values()]
    .sort((a, b) => roleOrder(a.role) - roleOrder(b.role) || a.alias.localeCompare(b.alias, 'zh-CN'))
    .map((row, index) => ({
      ...row,
      index: index + 1,
      total_hours: round(row.total_hours, 2),
      total_amount: round(row.total_amount, 2)
    }));

  return { stores, rows };
}

function buildPrimaryAttendanceMatrix(payroll) {
  const stores = PRIMARY_STORE_COLUMNS;
  const people = new Map();
  for (const item of payroll.filter(row => PRIMARY_STORE_IDS.has(row.store_id))) {
    const key = `${item.role}|${item.person}`;
    if (!people.has(key)) {
      people.set(key, {
        role: item.role,
        alias: item.person,
        name: PERSON_REAL_NAMES[item.person] || item.person,
        stores: {},
        total_hours: 0,
        total_amount: 0
      });
    }
    const person = people.get(key);
    person.stores[item.store_id] = {
      hours: item.actual_hours,
      amount: item.amount,
      tax: 0,
      net: item.amount
    };
    person.total_hours += item.actual_hours;
    person.total_amount += item.amount;
  }

  const rows = [...people.values()]
    .sort((a, b) => roleOrder(a.role) - roleOrder(b.role) || a.alias.localeCompare(b.alias, 'zh-CN'))
    .map((row, index) => ({
      ...row,
      index: index + 1,
      total_hours: round(row.total_hours, 2),
      total_amount: round(row.total_amount, 2)
    }));

  return { stores, rows };
}

function buildFinalPayrollMatrix(rows) {
  const stores = ATTENDANCE_STORE_COLUMNS;
  return {
    stores,
    rows: rows.map(row => ({
      role: row.role,
      index: row.index,
      alias: row.alias,
      name: row.name,
      stores: {
        official: {
          hours: Number(row.official_hours || 0),
          amount: Number(row.official_amount || 0),
          tax: Number(row.official_tax || 0),
          net: Number(row.official_net || 0)
        },
        sports_dou2: {
          hours: Number(row.sports_dou2_hours || 0),
          amount: Number(row.sports_dou2_amount || 0),
          tax: Number(row.sports_dou2_tax || 0),
          net: Number(row.sports_dou2_net || 0)
        },
        sports_video: {
          hours: Number(row.sports_video_hours || 0),
          amount: Number(row.sports_video_amount || 0),
          tax: Number(row.sports_video_tax || 0),
          net: Number(row.sports_video_net || 0)
        },
        sports_xhs: {
          hours: Number(row.sports_xhs_hours || 0),
          amount: Number(row.sports_xhs_amount || 0),
          tax: Number(row.sports_xhs_tax || 0),
          net: Number(row.sports_xhs_net || 0)
        },
        tmall: {
          hours: Number(row.tmall_hours || 0),
          amount: Number(row.tmall_amount || 0),
          tax: Number(row.tmall_tax || 0),
          net: Number(row.tmall_net || 0)
        },
        shooting: {
          hours: Number(row.shooting_hours || 0),
          amount: Number(row.shooting_amount || 0),
          tax: Number(row.shooting_tax || 0),
          net: Number(row.shooting_net || 0)
        }
      },
      total_hours: row.total_hours,
      total_amount: row.total_amount,
      total_tax: row.total_tax,
      total_net: row.total_net,
      rate: row.rate,
      remark: row.remark
    }))
  };
}

function roleOrder(role) {
  if (role === '主播') return 1;
  if (role === '中控') return 2;
  return 9;
}

function buildComparison(schedule, backend) {
  const map = new Map();
  const touch = (date, storeId, storeName) => {
    const key = `${date}|${storeId}`;
    if (!map.has(key)) map.set(key, { date, store_id: storeId, store_name: storeName, planned_person_hours: 0, backend_live_hours: 0, scheduled_actual_hours: 0, status: '正常' });
    return map.get(key);
  };
  for (const row of schedule) {
    const item = touch(row.date, row.store_id, row.store_name);
    item.planned_person_hours += row.planned_hours;
    item.scheduled_actual_hours += row.actual_hours;
  }
  for (const row of backend) touch(row.date, row.store_id, row.store_name).backend_live_hours += row.hours;
  return [...map.values()].map(item => {
    item.planned_person_hours = round(item.planned_person_hours, 2);
    item.backend_live_hours = round(item.backend_live_hours, 2);
    item.scheduled_actual_hours = round(item.scheduled_actual_hours, 2);
    if (item.backend_live_hours === 0 && item.planned_person_hours > 0) item.status = '缺后台';
    else if (item.backend_live_hours > 0 && item.planned_person_hours === 0) item.status = '后台多出';
    else if (item.scheduled_actual_hours === 0 && item.planned_person_hours > 0) item.status = '未匹配';
    return item;
  }).sort((a, b) => `${a.date}${a.store_name}`.localeCompare(`${b.date}${b.store_name}`, 'zh-CN'));
}

function buildAnomalies(schedule, backend, operations = []) {
  const out = [];
  for (const row of schedule) {
    if (row.backend_hours <= 0) out.push({ type: '排班未匹配后台', date: row.date, store_name: row.store_name, role: row.role, person: row.person, detail: `${row.start}-${row.end} 未匹配到后台直播` });
    if (row.warning_level !== '正常') out.push({ type: row.warning_level, date: row.date, store_name: row.store_name, role: row.role, person: row.person, detail: row.warning_detail });
  }
  for (const session of backend) {
    const matched = schedule.some(row => row.store_id === session.store_id && row.date === session.date && overlapHours(row.date, row.start, row.end, session.start, session.end) > 0);
    if (!matched) out.push({ type: '后台多出场次', date: session.date, store_name: session.store_name, role: '', person: '', detail: `${session.start} - ${session.end}，${session.hours.toFixed(2)} 小时` });
  }
  for (const item of operations) {
    const matched = schedule.some(row => row.store_id === item.store_id && row.date === item.date && row.person === item.person && overlapHours(row.date, row.start, row.end, item.start, item.end) > 0);
    if (!matched) out.push({ type: '经营表未匹配排班', date: item.date, store_name: item.store_name, role: '', person: item.person, detail: `${item.source_sheet} 第 ${item.source_row} 行，${item.operation_hours.toFixed(2)} 小时` });
  }
  return out;
}

function scheduleStatus(row) {
  if (row.backend_hours <= 0) return '未匹配后台';
  if (row.warning_level !== '正常') return row.warning_level;
  if (row.backend_hours + 0.05 < row.planned_hours) return '后台少于排班';
  return '已匹配';
}

function normalizeWarningDiffHours(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return DEFAULT_WARNING_DIFF_HOURS;
  return round(parsed, 2);
}

function normalizeTimeCalculationMode(value) {
  if (value === 'backend_allocation') return 'backend_allocation';
  return 'payroll_protection';
}

function allocateBackendHoursToSchedule(schedule, backend) {
  const allocation = new Map();
  const byStoreDayRole = new Map();

  for (const row of schedule) {
    if (!row.role || !row.person || row.exclude_from_payroll) continue;
    const key = `${row.store_id}|${row.date}|${row.role}`;
    if (!byStoreDayRole.has(key)) byStoreDayRole.set(key, []);
    byStoreDayRole.get(key).push(row);
  }

  for (const session of backend) {
    const sessionStart = parseDateTime(session.start);
    if (!sessionStart || !(session.hours > 0)) continue;
    const sessionEnd = new Date(sessionStart.getTime() + session.hours * 3600000);

    for (const role of ['主播', '中控']) {
      const candidates = byStoreDayRole.get(`${session.store_id}|${session.date}|${role}`) || [];
      const matches = candidates
        .map(row => {
          const interval = scheduleInterval(row);
          if (!interval) return null;
          const overlapMs = Math.max(0, Math.min(interval.end, sessionEnd) - Math.max(interval.start, sessionStart));
          if (overlapMs <= 0) return null;
          return { row, start: interval.start, end: interval.end, overlapMs, allocatedMs: overlapMs };
        })
        .filter(Boolean)
        .sort((a, b) => a.start - b.start);

      if (!matches.length) continue;
      allocateUncoveredSessionTime(matches, sessionStart, sessionEnd);
      normalizeSessionAllocation(matches, sessionEnd - sessionStart);

      for (const match of matches) {
        if (match.allocatedMs <= 0) continue;
        const current = allocation.get(match.row.id) || { hours: 0, sessionIds: [] };
        current.hours += match.allocatedMs / 3600000;
        if (!current.sessionIds.includes(session.id)) current.sessionIds.push(session.id);
        allocation.set(match.row.id, current);
      }
    }
  }

  for (const item of allocation.values()) item.hours = round(item.hours, 3);
  return allocation;
}

function allocateUncoveredSessionTime(matches, sessionStart, sessionEnd) {
  const clipped = matches.map(match => {
    match.coverStart = new Date(Math.max(match.start, sessionStart));
    match.coverEnd = new Date(Math.min(match.end, sessionEnd));
    return match;
  }).sort((a, b) => a.coverStart - b.coverStart);

  let cursor = sessionStart;
  let previous = null;
  for (const match of clipped) {
    if (match.coverStart > cursor) allocateGap(matches, previous, match, match.coverStart - cursor);
    if (match.coverEnd > cursor) cursor = match.coverEnd;
    previous = match;
  }
  if (sessionEnd > cursor) allocateGap(matches, previous, null, sessionEnd - cursor);
}

function allocateGap(matches, previous, next, gapMs) {
  if (gapMs <= 0) return;
  if (previous && next) {
    previous.allocatedMs += gapMs / 2;
    next.allocatedMs += gapMs / 2;
    return;
  }
  const target = previous || next || matches[0];
  if (target) target.allocatedMs += gapMs;
}

function normalizeSessionAllocation(matches, sessionMs) {
  const total = matches.reduce((sum, match) => sum + match.allocatedMs, 0);
  if (total <= sessionMs || total <= 0) return;
  const ratio = sessionMs / total;
  for (const match of matches) match.allocatedMs *= ratio;
}

function scheduleInterval(row) {
  if (!row?.date || !row?.start || !row?.end) return null;
  const start = combineDateTime(row.date, row.start);
  let end = combineDateTime(row.date, row.end);
  if (end <= start) end = new Date(end.getTime() + 86400000);
  return { start, end };
}

function chooseLegacyFinalHours(plannedHours, backendHours, operationHours, warningDiffHours) {
  return chooseBackendScheduleFinalHours(plannedHours, backendHours, warningDiffHours);
}

function chooseBackendAllocatedFinalHours(plannedHours, backendHours, operationHours, warningDiffHours) {
  return chooseBackendScheduleFinalHours(plannedHours, backendHours, warningDiffHours);
}

function choosePayrollProtectionFinalHours(row, warningDiffHours) {
  const planned = Number(row.planned_hours || 0);
  const backend = Number(row.backend_hours || 0);
  if (planned <= 0) return 0;
  if (row.store_id === 'official') return round(planned, 3);
  const extra = Math.max(backend - planned, 0);
  if (row.store_id === 'sports') return round(planned + Math.min(extra, 0.5), 3);
  if (extra > 0 && extra <= warningDiffHours) return round(planned + extra, 3);
  return round(planned, 3);
}

function chooseBackendScheduleFinalHours(plannedHours, backendHours, warningDiffHours) {
  const planned = Number(plannedHours || 0);
  const backend = Number(backendHours || 0);
  if (backend >= planned) return round(backend, 3);
  if (planned > 0 && planned - backend <= warningDiffHours) return round(planned, 3);
  return round(backend, 3);
}

function warningLevel(row, warningDiffHours = DEFAULT_WARNING_DIFF_HOURS) {
  if (Math.abs(Number(row.schedule_vs_backend_diff || 0)) > warningDiffHours) return '差异过大';
  if (row.operation_hours > 0 && Math.abs(Number(row.operation_vs_schedule_diff || 0)) > warningDiffHours) return '差异过大';
  if (row.backend_hours <= 0) return '缺后台';
  if (row.has_operation_source && row.operation_hours <= 0) return '缺经营数据';
  return '正常';
}

function warningDetail(row, warningDiffHours = DEFAULT_WARNING_DIFF_HOURS) {
  const parts = [];
  if (row.backend_hours <= 0) parts.push('未匹配到后台真实直播时段');
  if (Math.abs(Number(row.schedule_vs_backend_diff || 0)) > warningDiffHours) {
    parts.push(`后台与排班相差 ${Math.abs(row.schedule_vs_backend_diff).toFixed(2)} 小时`);
  }
  if (row.operation_hours > 0 && Math.abs(Number(row.operation_vs_schedule_diff || 0)) > warningDiffHours) {
    parts.push(`经营表与排班相差 ${Math.abs(row.operation_vs_schedule_diff).toFixed(2)} 小时`);
  }
  if (row.has_operation_source && row.operation_hours <= 0) parts.push('未匹配到经营表人员时长');
  if (!parts.length) parts.push('正常');
  return parts.join('；');
}

function rateFor(person, role) {
  return PERSON_RATES[person] ?? ROLE_RATES[role] ?? 0;
}

function overlapHours(date, start, end, backendStart, backendEnd) {
  const s = combineDateTime(date, start);
  let e = combineDateTime(date, end);
  if (e <= s) e = new Date(e.getTime() + 86400000);
  const bs = parseDateTime(backendStart);
  const be = parseDateTime(backendEnd);
  if (!bs || !be) return 0;
  return Math.max(0, Math.min(e, be) - Math.max(s, bs)) / 3600000;
}

function overlapDateTimeHours(startA, endA, startB, endB) {
  const aStart = parseDateTime(startA);
  const aEnd = parseDateTime(endA);
  const bStart = parseDateTime(startB);
  const bEnd = parseDateTime(endB);
  if (!aStart || !aEnd || !bStart || !bEnd) return 0;
  return Math.max(0, Math.min(aEnd, bEnd) - Math.max(aStart, bStart)) / 3600000;
}

function timePart(value) {
  const d = parseDateTime(value);
  if (!d) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function combineDateTime(dateText, hm) {
  const [y, m, d] = dateText.split('-').map(Number);
  const [h, mi] = hm.split(':').map(Number);
  return new Date(y, m - 1, d, h, mi, 0);
}

function hoursBetween(start, end) {
  const a = timeToMinutes(start);
  let b = timeToMinutes(end);
  if (b <= a) b += 1440;
  return (b - a) / 60;
}

function timeToMinutes(value) {
  const [h, m] = value.split(':').map(Number);
  return h * 60 + m;
}

function findHeader(headers, names) {
  for (const name of names) {
    const index = headers.findIndex(header => header.includes(name));
    if (index >= 0) return index;
  }
  return -1;
}

function findDurationSecondsHeader(headers) {
  return headers.findIndex(header => ['直播时长（秒）', '直播时长(秒)', '直播间时长（秒）'].includes(header));
}

function isOperationContinuationRow(row, personColumnIndexes, structureColumnIndexes) {
  for (const index of structureColumnIndexes) {
    if (parseNumber(row[index]) > 0) return false;
  }
  let hasPersonHours = false;
  for (const index of personColumnIndexes) {
    if (parseNumber(row[index]) > 0) {
      hasPersonHours = true;
      break;
    }
  }
  if (!hasPersonHours) return false;

  for (let index = 0; index < row.length; index++) {
    if (personColumnIndexes.has(index) || structureColumnIndexes.has(index)) continue;
    const text = cleanText(row[index]);
    if (!text || parseNumber(text) > 0) continue;
    return true;
  }
  return false;
}

function parseDateTime(value) {
  if (value instanceof Date && !Number.isNaN(value.getTime())) return value;
  const raw = cleanText(value).replace(/\//g, '-');
  if (!raw) return null;
  const match = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?/);
  if (!match) return null;
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4] || 0), Number(match[5] || 0), Number(match[6] || 0));
}

function asDate(value) {
  if (typeof value === 'number' && value > 30000 && value < 70000) {
    return new Date(Math.round((value - 25569) * 86400000));
  }
  const parsed = parseDateTime(value);
  return parsed && parsed.getFullYear() > 2000 ? parsed : null;
}

function parseNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const n = Number(cleanText(value).replace(/,/g, ''));
  return Number.isFinite(n) ? n : 0;
}

function cellNumber(cell) {
  const value = cell.value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (value && typeof value === 'object' && typeof value.result === 'number') return value.result;
  const text = cell.text && cell.text !== '[object Object]' ? cell.text : '';
  const n = Number(cleanText(text).replace(/,/g, ''));
  return Number.isFinite(n) ? n : 0;
}

function cellDisplayValue(cell) {
  const value = cell.value;
  if (value == null) return '';
  if (typeof value === 'number') return round(value, 6);
  if (value instanceof Date) return formatDate(value);
  if (typeof value === 'object') {
    if ('result' in value && value.result != null) return typeof value.result === 'number' ? round(value.result, 6) : value.result;
    if (cell.text && cell.text !== '[object Object]') {
      const n = Number(cell.text);
      return Number.isFinite(n) ? round(n, 6) : cell.text;
    }
    if ('text' in value) return value.text;
    if ('richText' in value) return value.richText.map(part => part.text).join('');
    return '';
  }
  return value;
}

function cellValue(ws, r, c) {
  const cell = ws.getRow(r).getCell(c);
  const value = cell.value;
  if (value == null) return '';
  if (value instanceof Date) return value;
  if (typeof value === 'object') {
    if ('result' in value) return value.result;
    if ('text' in value) return value.text;
    if ('richText' in value) return value.richText.map(part => part.text).join('');
    if ('hyperlink' in value && 'text' in value) return value.text;
  }
  return value;
}

function firstText(values) {
  return values.map(cleanText).find(Boolean) || '';
}

function normalizeRole(value) {
  const text = cleanText(value);
  if (text.includes('主播')) return '主播';
  if (text.includes('中控')) return '中控';
  return text;
}

function cleanText(value) {
  if (value == null) return '';
  if (typeof value === 'number' && Number.isInteger(value)) return String(value);
  return String(value).trim();
}

function normalizeText(value) {
  return cleanText(value).replace(/：/g, ':').replace(/[－—～~]/g, '-').replace(/\u3000/g, ' ');
}

function canonicalPerson(value) {
  const text = normalizeText(value).replace(/[(（].*?[)）]/g, '').replace(/\s+/g, '').replace(/^[:：-]+|[:：-]+$/g, '');
  return ALIASES[text] || text;
}

function formatDate(value) {
  const d = value instanceof Date ? value : parseDateTime(value);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function formatDateTime(value) {
  const d = value instanceof Date ? value : parseDateTime(value);
  return `${formatDate(d)} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function sum(rows, key) {
  return rows.reduce((total, row) => total + Number(row[key] || 0), 0);
}

function round(value, digits) {
  const m = 10 ** digits;
  return Math.round((Number(value) || 0) * m) / m;
}

function roundUp(value, digits) {
  const m = 10 ** digits;
  return Math.ceil((Number(value) || 0) * m) / m;
}

function cryptoId() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function colLetter(column) {
  let n = column;
  let out = '';
  while (n > 0) {
    const mod = (n - 1) % 26;
    out = String.fromCharCode(65 + mod) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}
