import fs from 'node:fs/promises';
import companyPolicy from '../../company_policy.cjs';

const OAPI_BASE = 'https://oapi.dingtalk.com';
const OPEN_API_BASE = process.env.DINGTALK_OPEN_API_BASE || 'https://api.dingtalk.com';
const DEFAULT_OPERATOR_USER_ID = companyPolicy.parttime?.DEFAULT_OPERATOR_USER_ID ?? '';
const DEFAULT_TARGETS = {
  operation: {
    name: '经营/核算',
    baseName: '直播经营数据',
    baseId: '',
    sheets: [
      { key: 'official_operation', storeId: 'official', name: '官旗直播间数据统计' },
      { key: 'sports_operation', storeId: 'sports', name: '运动店直播间数据统计' }
    ]
  },
  backend: {
    name: '后台真实数据',
    baseName: '直播后台数据',
    baseId: '',
    sheets: [
      { key: 'backend_live_detail', name: '直播明细' }
    ]
  }
};
const STORE_NAMES = {
  official: '抖音官方旗舰店',
  sports: '抖音运动旗舰店'
};
const PERSON_ALIASES = companyPolicy.parttime?.PERSON_ALIASES ?? {};
const REAL_NAME_TO_ALIAS = companyPolicy.parttime?.REAL_NAME_TO_ALIAS ?? {};

export async function getAiTableStatus() {
  const ctx = await createAiTableContext();
  const bases = await Promise.all(Object.entries(ctx.targets).map(async ([key, target]) => inspectBase(ctx, key, target, false)));
  return {
    ok: bases.every(base => base.ok),
    generated_at: new Date().toISOString(),
    operator_user_id: ctx.config.operatorUserId || '',
    operator_id_resolved: Boolean(ctx.operatorId),
    open_api_base: ctx.config.openApiBase,
    bases
  };
}

export async function previewAiTableSources(options = {}) {
  const ctx = await createAiTableContext();
  const limit = clamp(Number(options.limit || 10), 1, 100);
  const bases = await Promise.all(Object.entries(ctx.targets).map(async ([key, target]) => inspectBase(ctx, key, target, true, limit)));
  return {
    ok: bases.every(base => base.ok),
    generated_at: new Date().toISOString(),
    preview_limit: limit,
    bases
  };
}

export async function queryAiTableOperationRows(options = {}) {
  const ctx = await createAiTableContext();
  const scheduleRows = Array.isArray(options.scheduleRows) ? options.scheduleRows : [];
  const scheduleMonths = new Set(scheduleRows.map(row => String(row.date || '').slice(0, 7)).filter(Boolean));
  const operationTarget = ctx.targets.operation;
  const sheets = await listSheets(ctx, operationTarget.baseId);
  const items = [];

  for (const sheetSpec of operationTarget.sheets) {
    const matched = resolveSheet(sheets, sheetSpec.name);
    if (!matched) throw new Error(`钉钉 AI 表格未找到经营/核算数据表：${sheetSpec.name}`);
    const records = await listAllRecords(ctx, operationTarget.baseId, matched.id || matched.name || sheetSpec.name);
    records.forEach((record, index) => {
      items.push(...operationRecordToItems(record, {
        index,
        sheetName: matched.name || sheetSpec.name,
        storeId: sheetSpec.storeId
      }));
    });
  }

  const filtered = scheduleMonths.size
    ? items.filter(item => scheduleMonths.has(item.date.slice(0, 7)))
    : items;
  return allocateOperationItemsToSchedule(filtered, scheduleRows);
}

export async function previewAiTableBackendRows(options = {}) {
  const ctx = await createAiTableContext();
  const backendTarget = ctx.targets.backend;
  const sheetSpec = backendTarget.sheets[0];
  const sheets = await listSheets(ctx, backendTarget.baseId);
  const matched = resolveSheet(sheets, sheetSpec.name);
  if (!matched) throw new Error(`钉钉 AI 表格未找到后台真实数据表：${sheetSpec.name}`);

  const limit = clamp(Number(options.limit || 50), 1, 500);
  const month = normalizeMonth(options.month);
  const records = await listAllRecords(ctx, backendTarget.baseId, matched.id || matched.name || sheetSpec.name);
  const rows = records
    .map((record, index) => backendRecordToRow(record, { index, sheetName: matched.name || sheetSpec.name }))
    .filter(Boolean)
    .filter(row => !month || row.date.startsWith(month))
    .sort((a, b) => String(a.start).localeCompare(String(b.start)))
    .slice(0, limit);

  const stores = rows.reduce((acc, row) => {
    acc[row.store_id] = (acc[row.store_id] || 0) + 1;
    return acc;
  }, {});
  return {
    ok: true,
    generated_at: new Date().toISOString(),
    source: {
      base_name: backendTarget.baseName,
      base_id: backendTarget.baseId,
      sheet_name: matched.name || sheetSpec.name,
      sheet_id: matched.id || ''
    },
    month,
    total_records: records.length,
    rows_count: rows.length,
    stores,
    columns: [
      { key: 'date', label: '日期' },
      { key: 'store_id', label: '店铺ID' },
      { key: 'store_name', label: '店铺' },
      { key: 'title', label: '主播昵称' },
      { key: 'start', label: '直播开始' },
      { key: 'end', label: '直播结束' },
      { key: 'hours', label: '直播小时' },
      { key: 'source_row', label: '来源行' }
    ],
    rows
  };
}

async function inspectBase(ctx, key, target, includeRecords, limit = 10) {
  const out = {
    key,
    name: target.name,
    base_name: target.baseName,
    base_id: target.baseId,
    ok: false,
    sheets_count: 0,
    target_sheets: []
  };
  try {
    const sheets = await listSheets(ctx, target.baseId);
    out.sheets_count = sheets.length;
    out.available_sheets = sheets.map(sheet => ({ id: sheet.id || '', name: sheet.name || '' }));
    for (const sheetSpec of target.sheets) {
      const matched = resolveSheet(sheets, sheetSpec.name);
      const sheetOut = {
        key: sheetSpec.key,
        name: sheetSpec.name,
        store_id: sheetSpec.storeId || '',
        exists: Boolean(matched),
        sheet_id: matched?.id || '',
        matched_name: matched?.name || ''
      };
      if (matched) {
        const sheetIdOrName = matched.id || matched.name || sheetSpec.name;
        const fields = await listFields(ctx, target.baseId, sheetIdOrName);
        sheetOut.fields_count = fields.length;
        sheetOut.fields = fields.map(field => ({
          id: field.id || '',
          name: field.name || '',
          type: field.type || ''
        }));
        if (includeRecords) {
          const records = await listRecords(ctx, target.baseId, sheetIdOrName, limit);
          sheetOut.records_count_preview = records.length;
          sheetOut.records = records.map(record => normalizeRecordPreview(record, fields));
        }
      }
      out.target_sheets.push(sheetOut);
    }
    out.ok = out.target_sheets.every(sheet => sheet.exists);
  } catch (err) {
    out.error = err.message || String(err);
  }
  return out;
}

async function createAiTableContext() {
  const config = await loadDingTalkAiTableConfig();
  const accessToken = await getAccessToken({
    appKey: config.aiAppKey,
    appSecret: config.aiAppSecret
  });
  const userLookupToken = await getAccessToken({
    appKey: config.userLookupAppKey,
    appSecret: config.userLookupAppSecret
  });
  const operatorId = config.operatorId || await getUserUnionId(userLookupToken, config.operatorUserId);
  if (!operatorId) {
    throw new Error('钉钉 AI 表格未获取到 operatorId，请配置 DINGTALK_AI_TABLE_OPERATOR_ID 或确认 operator userId 可查询 unionId');
  }
  return { accessToken, operatorId, config, targets: config.targets };
}

async function loadDingTalkAiTableConfig() {
  const configFile = process.env.DINGTALK_CONFIG_FILE;
  let fileConfig = {};
  if (configFile) {
    fileConfig = JSON.parse(await fs.readFile(configFile, 'utf8'));
  }
  const defaultAppKey = process.env.DINGTALK_APP_KEY || fileConfig.appKey || fileConfig.clientId;
  const defaultAppSecret = process.env.DINGTALK_APP_SECRET || fileConfig.appSecret || fileConfig.clientSecret;
  if (!defaultAppKey || !defaultAppSecret) {
    throw new Error('钉钉未配置：请设置 DINGTALK_CONFIG_FILE，或设置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET');
  }

  const aiTableConfig = fileConfig.aiTable || fileConfig.aiTables || {};
  const aiAppKey = process.env.DINGTALK_AI_TABLE_APP_KEY || aiTableConfig.appKey || aiTableConfig.clientId || defaultAppKey;
  const aiAppSecret = process.env.DINGTALK_AI_TABLE_APP_SECRET || aiTableConfig.appSecret || aiTableConfig.clientSecret || defaultAppSecret;
  return {
    aiAppKey,
    aiAppSecret,
    userLookupAppKey: defaultAppKey,
    userLookupAppSecret: defaultAppSecret,
    operatorUserId: process.env.DINGTALK_AI_TABLE_OPERATOR_USER_ID || aiTableConfig.operatorUserId || DEFAULT_OPERATOR_USER_ID,
    operatorId: process.env.DINGTALK_AI_TABLE_OPERATOR_ID || aiTableConfig.operatorId || '',
    openApiBase: process.env.DINGTALK_OPEN_API_BASE || aiTableConfig.openApiBase || OPEN_API_BASE,
    targets: mergeTargets(aiTableConfig.targets || aiTableConfig)
  };
}

function mergeTargets(configTargets = {}) {
  const operation = configTargets.operation || {};
  const backend = configTargets.backend || {};
  return {
    operation: {
      ...DEFAULT_TARGETS.operation,
      ...operation,
      sheets: operation.sheets || [
        {
          ...DEFAULT_TARGETS.operation.sheets[0],
          name: operation.officialSheet || DEFAULT_TARGETS.operation.sheets[0].name
        },
        {
          ...DEFAULT_TARGETS.operation.sheets[1],
          name: operation.sportsSheet || DEFAULT_TARGETS.operation.sheets[1].name
        }
      ]
    },
    backend: {
      ...DEFAULT_TARGETS.backend,
      ...backend,
      sheets: backend.sheets || [
        {
          ...DEFAULT_TARGETS.backend.sheets[0],
          name: backend.sheet || DEFAULT_TARGETS.backend.sheets[0].name
        }
      ]
    }
  };
}

async function getAccessToken(config) {
  const url = `${OAPI_BASE}/gettoken?appkey=${encodeURIComponent(config.appKey)}&appsecret=${encodeURIComponent(config.appSecret)}`;
  const data = await requestJson(url);
  if (data.errcode !== 0 || !data.access_token) {
    throw new Error(`钉钉 access_token 获取失败：${data.errmsg || data.errcode}`);
  }
  return data.access_token;
}

async function getUserUnionId(accessToken, userId) {
  if (!userId) return '';
  const data = await requestJson(`${OAPI_BASE}/topapi/v2/user/get?access_token=${encodeURIComponent(accessToken)}`, {
    method: 'POST',
    body: { userid: userId }
  });
  if (data.errcode !== 0) throw new Error(`钉钉用户 unionId 获取失败：${data.errmsg || data.errcode}`);
  return data.result?.unionid || data.result?.unionId || '';
}

async function listSheets(ctx, baseId) {
  const data = await requestNotable(ctx, `/v1.0/notable/bases/${encodePath(baseId)}/sheets`);
  return extractList(data);
}

async function listFields(ctx, baseId, sheetIdOrName) {
  const data = await requestNotable(ctx, `/v1.0/notable/bases/${encodePath(baseId)}/sheets/${encodePath(sheetIdOrName)}/fields`);
  return extractList(data);
}

async function listRecords(ctx, baseId, sheetIdOrName, limit) {
  const records = [];
  let nextToken = '';
  do {
    const data = await requestNotable(ctx, `/v1.0/notable/bases/${encodePath(baseId)}/sheets/${encodePath(sheetIdOrName)}/records/list`, {
      method: 'POST',
      body: {
        maxResults: Math.min(100, Math.max(1, limit - records.length)),
        ...(nextToken ? { nextToken } : {})
      }
    });
    records.push(...extractList(data));
    nextToken = data.nextToken || data.next_token || data.result?.nextToken || data.data?.nextToken || '';
  } while (nextToken && records.length < limit);
  return records.slice(0, limit);
}

async function listAllRecords(ctx, baseId, sheetIdOrName) {
  const records = [];
  let nextToken = '';
  do {
    const data = await requestNotable(ctx, `/v1.0/notable/bases/${encodePath(baseId)}/sheets/${encodePath(sheetIdOrName)}/records/list`, {
      method: 'POST',
      body: {
        maxResults: 100,
        ...(nextToken ? { nextToken } : {})
      }
    });
    records.push(...extractList(data));
    nextToken = data.nextToken || data.next_token || data.result?.nextToken || data.data?.nextToken || '';
  } while (nextToken);
  return records;
}

async function requestNotable(ctx, path, options = {}) {
  const url = new URL(path, ctx.config.openApiBase);
  url.searchParams.set('operatorId', ctx.operatorId);
  return requestJson(url.toString(), {
    method: options.method || 'GET',
    headers: {
      'x-acs-dingtalk-access-token': ctx.accessToken
    },
    body: options.body
  });
}

async function requestJson(url, options = {}) {
  let lastError;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const init = { method: options.method || 'GET', headers: { ...(options.headers || {}) } };
      if (options.body) {
        init.headers['content-type'] = 'application/json';
        init.body = JSON.stringify(options.body);
      }
      const res = await fetch(url, init);
      const text = await res.text();
      let data;
      try {
        data = JSON.parse(text);
      } catch {
        throw new Error(`钉钉接口返回非 JSON：HTTP ${res.status}`);
      }
      if (!res.ok) {
        if (attempt < 3 && [502, 503, 504].includes(res.status)) {
          await sleep(500 * attempt);
          continue;
        }
        throw new Error(`钉钉接口 HTTP 失败：${res.status} ${data.errmsg || data.message || ''}`);
      }
      if (data.errcode && data.errcode !== 0) throw new Error(`钉钉接口失败：${data.errmsg || data.errcode}`);
      if (data.code && !['0', 'OK', 'Success'].includes(String(data.code))) throw new Error(`钉钉接口失败：${data.message || data.code}`);
      return data;
    } catch (err) {
      lastError = err;
      if (attempt < 3 && /fetch failed|ECONNRESET|ETIMEDOUT|temporary/i.test(String(err.message || err))) {
        await sleep(500 * attempt);
        continue;
      }
      throw err;
    }
  }
  throw lastError;
}

function extractList(data) {
  const candidates = [
    data.value,
    data.values,
    data.records,
    data.fields,
    data.sheets,
    data.data?.value,
    data.data?.records,
    data.data?.fields,
    data.result?.value,
    data.result?.records,
    data.result?.fields,
    data.result?.list
  ];
  return candidates.find(Array.isArray) || [];
}

function resolveSheet(sheets, name) {
  const expected = String(name || '').trim();
  return sheets.find(sheet => String(sheet.name || '').trim() === expected)
    || sheets.find(sheet => String(sheet.id || '').trim() === expected)
    || null;
}

function normalizeRecordPreview(record, fields) {
  const rawFields = record.fields || record.values || {};
  const values = {};
  for (const field of fields) {
    const name = field.name || field.id;
    if (!name) continue;
    values[name] = rawFields[field.id] ?? rawFields[field.name] ?? '';
  }
  for (const [key, value] of Object.entries(rawFields)) {
    if (!(key in values)) values[key] = value;
  }
  return {
    id: record.id || record.recordId || '',
    fields: values
  };
}

function operationRecordToItems(record, meta) {
  const fields = record.fields || record.values || {};
  const recordId = record.id || record.recordId || `${meta.sheetName}-${meta.index + 1}`;
  const storeId = meta.storeId;
  if (storeId === 'official') {
    const date = normalizeDateOnly(fields['日期']);
    const hours = parseNumber(fields['直播时长（小时）']);
    if (!date || !hours) return [];
    const anchor = normalizePerson(fields['主播名称l']);
    const control = normalizePerson(fields['中控名称']);
    return peopleItems([
      { role: '主播', person: anchor },
      { role: '中控', person: control }
    ], { recordId, date, hours, storeId, sheetName: meta.sheetName, sourceRow: meta.index + 1, pairedPeople: [anchor, control].filter(Boolean) });
  }
  if (storeId === 'sports') {
    const date = normalizeDateOnly(fields['标题']);
    const hours = parseNumber(fields['直播时长']);
    if (!date || !hours) return [];
    const interval = parseInterval(fields['直播时段']);
    return peopleItems([
      { role: '主播', person: fields['主播'] },
      { role: '中控', person: fields['中控'] }
    ], {
      recordId,
      date,
      hours,
      storeId,
      sheetName: meta.sheetName,
      sourceRow: meta.index + 1,
      startTime: interval?.[0] || '',
      endTime: interval?.[1] || ''
    });
  }
  return [];
}

function peopleItems(people, common) {
  return people
    .map((item, index) => ({
      id: `ai-${common.storeId}-${common.recordId}-${index}`,
      date: common.date,
      store_id: common.storeId,
      store_name: STORE_NAMES[common.storeId] || common.storeId,
      role: item.role,
      person: normalizePerson(item.person),
      hours: common.hours,
      start_time: common.startTime || '',
      end_time: common.endTime || '',
      paired_people: common.pairedPeople || [],
      source_sheet: common.sheetName,
      source_row: common.sourceRow
    }))
    .filter(item => item.person);
}

function allocateOperationItemsToSchedule(items, scheduleRows) {
  const out = [];
  const usedScheduleKeys = new Set();
  for (const item of items) {
    const candidates = scheduleRows.filter(row =>
      row.store_id === item.store_id
      && row.date === item.date
      && row.person === item.person
    );
    const rows = item.start_time && item.end_time
      ? chooseTimedScheduleRows(item, candidates, usedScheduleKeys)
      : chooseUntimedScheduleRows(item, candidates, scheduleRows, usedScheduleKeys);

    if (!rows.length) {
      if (candidates.length) continue;
      const [start, end] = item.start_time && item.end_time ? [item.start_time, item.end_time] : ['', ''];
      out.push(buildOperationRow(item, start, end, item.hours, item.hours, 'unmatched'));
      continue;
    }

    const totalPlanned = rows.reduce((sum, row) => sum + Math.max(Number(row.planned_hours || 0), 0), 0);
    rows.forEach((row, index) => {
      const ratio = totalPlanned > 0 ? Math.max(Number(row.planned_hours || 0), 0) / totalPlanned : 1 / rows.length;
      const hours = index === rows.length - 1
        ? item.hours - out
          .filter(existing => existing._source_item_id === item.id)
          .reduce((sum, existing) => sum + existing.operation_hours, 0)
        : round(item.hours * ratio, 3);
      out.push(buildOperationRow(item, row.start, row.end, hours, item.hours, row.id));
      usedScheduleKeys.add(scheduleUseKey(row));
    });
  }
  return out.map(({ _source_item_id, ...row }) => row);
}

function chooseTimedScheduleRows(item, candidates, usedScheduleKeys) {
  const matched = candidates.filter(row => timeOverlapHours(row.start, row.end, item.start_time, item.end_time) > 0);
  const rows = matched.length ? matched : candidates;
  return rows.filter(row => !usedScheduleKeys.has(scheduleUseKey(row)));
}

function chooseUntimedScheduleRows(item, candidates, scheduleRows, usedScheduleKeys) {
  if (!candidates.length) return [];
  const paired = item.paired_people || [];
  const pairMatched = paired.length > 1
    ? candidates.filter(row => hasPairedScheduleRow(row, paired, scheduleRows))
    : [];
  const pool = (pairMatched.length ? pairMatched : candidates)
    .filter(row => !usedScheduleKeys.has(scheduleUseKey(row)));
  if (!pool.length) return [];
  const scored = pool
    .map(row => ({
      row,
      score: Math.abs(Number(row.planned_hours || 0) - item.hours),
      pair: pairMatched.includes(row) ? 0 : 1
    }))
    .sort((a, b) => a.pair - b.pair || a.score - b.score || String(a.row.start).localeCompare(String(b.row.start)));
  return scored.length ? [scored[0].row] : [];
}

function hasPairedScheduleRow(row, pairedPeople, scheduleRows) {
  return scheduleRows.some(other =>
    other !== row
    && other.store_id === row.store_id
    && other.date === row.date
    && other.start === row.start
    && other.end === row.end
    && pairedPeople.includes(other.person)
  );
}

function scheduleUseKey(row) {
  return `${row.id || ''}|${row.date}|${row.store_id}|${row.person}|${row.start}|${row.end}`;
}

function buildOperationRow(item, startTime, endTime, operationHours, sessionHours, matchKey) {
  return {
    _source_item_id: item.id,
    id: `${item.id}-${matchKey}`,
    date: item.date,
    store_id: item.store_id,
    store_name: item.store_name,
    person: item.person,
    start: startTime ? `${item.date} ${startTime}` : '',
    end: endTime ? `${item.date} ${endTime}` : '',
    session_hours: round(sessionHours, 3),
    operation_hours: round(operationHours, 3),
    source_sheet: item.source_sheet,
    source_row: item.source_row,
    source_column: ''
  };
}

function backendRecordToRow(record, meta) {
  const fields = record.fields || record.values || {};
  const start = normalizeDateTime(fields['直播开始时间']);
  if (!start) return null;
  const endFromField = normalizeDateTime(fields['直播结束时间']);
  const minutes = parseNumber(fields['直播时长(分钟)']);
  const end = endFromField || addMinutes(start, minutes);
  const hours = minutes > 0 ? minutes / 60 : diffHours(start, end);
  if (!hours || hours <= 0) return null;
  const title = stringifyCell(fields['主播昵称']);
  const storeId = inferBackendStoreId(fields);
  return {
    id: `ai-backend-${record.id || record.recordId || meta.index + 1}`,
    date: start.slice(0, 10),
    store_id: storeId,
    store_name: STORE_NAMES[storeId] || (storeId === 'unknown' ? '待识别' : storeId),
    title,
    start,
    end,
    hours: round(hours, 3),
    source_sheet: meta.sheetName,
    source_row: meta.index + 1,
    raw_store_hint: [fields['主播昵称'], fields['主播抖音号']].map(stringifyCell).filter(Boolean).join(' / ')
  };
}

function inferBackendStoreId(fields) {
  const text = [
    fields['主播昵称'],
    fields['主播抖音号'],
    fields['店铺'],
    fields['平台'],
    fields['账号']
  ].map(stringifyCell).join('|');
  if (/运动|小红书/.test(text)) return 'sports';
  if (/官旗|官方/.test(text)) return 'official';
  return 'unknown';
}

function normalizePerson(value) {
  const raw = typeof value === 'object' && value ? value.name ?? value.text ?? value.value ?? '' : value;
  const text = String(raw ?? '').trim();
  if (!text) return '';
  return PERSON_ALIASES[text] || REAL_NAME_TO_ALIAS[text] || text;
}

function normalizeDateOnly(value) {
  if (value == null || value === '') return '';
  if (typeof value === 'number' || /^\d{10,}$/.test(String(value))) {
    const date = new Date(Number(value));
    if (Number.isNaN(date.getTime())) return '';
    return `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}-${String(date.getUTCDate()).padStart(2, '0')}`;
  }
  const text = String(value).trim();
  const match = text.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
  if (match) return `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')}`;
  return '';
}

function normalizeDateTime(value) {
  if (value == null || value === '') return '';
  if (typeof value === 'number' || /^\d{10,}$/.test(String(value))) {
    const date = new Date(Number(value));
    if (Number.isNaN(date.getTime())) return '';
    return formatLocalDateTime(date);
  }
  const text = String(value).trim();
  const match = text.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (match) {
    return `${match[1]}-${match[2].padStart(2, '0')}-${match[3].padStart(2, '0')} ${match[4].padStart(2, '0')}:${match[5]}:${(match[6] || '00').padStart(2, '0')}`;
  }
  return '';
}

function parseNumber(value) {
  if (value == null || value === '') return 0;
  if (typeof value === 'number') return Number.isFinite(value) ? value : 0;
  const text = String(value).replace(/,/g, '').replace('%', '').trim();
  const match = text.match(/-?\d+(?:\.\d+)?/);
  return match ? Number(match[0]) : 0;
}

function normalizeMonth(value) {
  const text = String(value || '').trim();
  return /^\d{4}-\d{2}$/.test(text) ? text : '';
}

function stringifyCell(value) {
  if (value == null) return '';
  if (typeof value === 'object') return String(value.name ?? value.text ?? value.value ?? JSON.stringify(value));
  return String(value);
}

function addMinutes(dateTime, minutes) {
  if (!minutes) return '';
  const date = parseLocalDateTime(dateTime);
  if (!date) return '';
  return formatLocalDateTime(new Date(date.getTime() + minutes * 60000));
}

function diffHours(start, end) {
  const a = parseLocalDateTime(start);
  const b = parseLocalDateTime(end);
  if (!a || !b) return 0;
  return Math.max(0, (b.getTime() - a.getTime()) / 3600000);
}

function parseLocalDateTime(value) {
  const match = String(value || '').match(/(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?/);
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), Number(match[4]), Number(match[5]), Number(match[6] || 0));
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatLocalDateTime(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
}

function parseInterval(value) {
  const text = String(value || '');
  const match = text.match(/(\d{1,2})[:：](\d{2})\s*[-~—至到]\s*(\d{1,2})[:：](\d{2})/);
  if (!match) return null;
  return [`${match[1].padStart(2, '0')}:${match[2]}`, `${match[3].padStart(2, '0')}:${match[4]}`];
}

function timeOverlapHours(aStart, aEnd, bStart, bEnd) {
  const a = timeRangeMinutes(aStart, aEnd);
  const b = timeRangeMinutes(bStart, bEnd);
  const overlap = Math.max(0, Math.min(a.end, b.end) - Math.max(a.start, b.start));
  return overlap / 60;
}

function timeRangeMinutes(start, end) {
  const startMinutes = timeToMinutes(start);
  let endMinutes = timeToMinutes(end);
  if (endMinutes <= startMinutes) endMinutes += 24 * 60;
  return { start: startMinutes, end: endMinutes };
}

function timeToMinutes(value) {
  const match = String(value || '').match(/(\d{1,2})[:：](\d{2})/);
  if (!match) return 0;
  return Number(match[1]) * 60 + Number(match[2]);
}

function round(value, digits = 3) {
  const factor = 10 ** digits;
  return Math.round((Number(value) + Number.EPSILON) * factor) / factor;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function encodePath(value) {
  return encodeURIComponent(String(value || ''));
}

function clamp(value, min, max) {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.min(max, Math.trunc(value)));
}
