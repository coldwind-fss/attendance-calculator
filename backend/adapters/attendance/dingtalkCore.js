import ExcelJS from 'exceljs';
import fs from 'node:fs/promises';

const OAPI_BASE = 'https://oapi.dingtalk.com';
const MAX_USER_CHUNK = 50;
const MAX_DAY_CHUNK = 7;

export async function queryDingTalkAttendanceSource(options = {}) {
  const config = await loadDingTalkConfig();
  const accessToken = await getDingTalkAccessToken(config);
  const month = normalizeMonth(options.month);
  const rootDeptId = Number(options.deptId || process.env.DINGTALK_DEFAULT_DEPT_ID || config.defaultDeptId || 1) || 1;
  const includeChildren = options.includeChildren !== false;
  const deptIds = includeChildren ? await collectDepartmentIds(accessToken, rootDeptId) : [rootDeptId];
  const users = await collectUsers(accessToken, deptIds);
  const records = await collectAttendanceRecords(accessToken, users, month);
  const rows = records
    .map(record => normalizeAttendanceRecord(record, users))
    .sort((a, b) => `${a.date}${a.user_name}${a.check_time}`.localeCompare(`${b.date}${b.user_name}${b.check_time}`, 'zh-CN'));
  const monthly = buildMonthlySheet(sourceMonthContext(month), users, rows);

  return {
    month,
    root_dept_id: rootDeptId,
    include_children: includeChildren,
    dept_count: deptIds.length,
    user_count: users.length,
    record_count: rows.length,
    generated_at: new Date().toISOString(),
    monthly,
    rows
  };
}

// 仅查询通讯录，用于把考勤结果中的姓名匹配到钉钉 UserId；不会发送消息。
export async function resolveDingTalkUsersByName(names, options = {}) {
  const config = await loadDingTalkConfig();
  const accessToken = await getDingTalkAccessToken(config);
  const rootDeptId = Number(options.deptId || process.env.DINGTALK_DEFAULT_DEPT_ID || config.defaultDeptId || 1) || 1;
  const includeChildren = options.includeChildren !== false;
  const deptIds = includeChildren ? await collectDepartmentIds(accessToken, rootDeptId) : [rootDeptId];
  const users = await collectUsers(accessToken, deptIds);
  const byName = new Map();
  for (const user of users) {
    for (const key of normalizedNameCandidates(user.name)) {
      const list = byName.get(key) || [];
      list.push(user);
      byName.set(key, list);
    }
  }
  return [...new Set((names || []).map(name => String(name || '').trim()).filter(Boolean))].map(employeeName => {
    const candidates = byName.get(normalizeUserName(employeeName)) || [];
    if (candidates.length === 1) {
      return {
        employee_name: employeeName,
        recipient_user_id: candidates[0].user_id,
        display_name: candidates[0].name,
        status: 'matched'
      };
    }
    return {
      employee_name: employeeName,
      recipient_user_id: '',
      display_name: employeeName,
      status: candidates.length ? 'ambiguous' : 'missing',
      candidates: candidates.map(item => ({ name: item.name, user_id: item.user_id }))
    };
  });
}

export async function exportDingTalkAttendanceSourceXlsx(source) {
  const wb = new ExcelJS.Workbook();
  wb.creator = '直播考勤统计计算系统';
  wb.created = new Date();

  appendMonthlySheet(wb, source);
  appendOriginalSheet(wb, source);

  const summary = wb.addWorksheet('查询摘要');
  summary.columns = [
    { header: '项目', key: 'label', width: 22 },
    { header: '值', key: 'value', width: 32 }
  ];
  summary.addRows([
    { label: '月份', value: source.month },
    { label: '根部门ID', value: source.root_dept_id },
    { label: '包含子部门', value: source.include_children ? '是' : '否' },
    { label: '部门数', value: source.dept_count },
    { label: '人员数', value: source.user_count },
    { label: '打卡记录数', value: source.record_count },
    { label: '生成时间', value: source.generated_at }
  ]);
  styleHeader(summary);

  const ws = wb.addWorksheet('钉钉打卡明细', { views: [{ state: 'frozen', ySplit: 1, xSplit: 3 }] });
  ws.columns = [
    { header: '姓名', key: 'user_name', width: 14 },
    { header: 'UserID', key: 'user_id', width: 24 },
    { header: '日期', key: 'date', width: 12 },
    { header: '打卡时间', key: 'check_time', width: 21 },
    { header: '打卡类型', key: 'check_type', width: 12 },
    { header: '时间结果', key: 'time_result', width: 12 },
    { header: '位置结果', key: 'location_result', width: 12 },
    { header: '考勤组ID', key: 'group_id', width: 14 },
    { header: '班次ID', key: 'class_id', width: 14 },
    { header: '计划打卡时间', key: 'plan_check_time', width: 21 },
    { header: '工作日', key: 'work_date', width: 12 },
    { header: 'WiFi名称', key: 'base_ssid', width: 18 },
    { header: 'WiFi MAC', key: 'base_mac_addr', width: 20 },
    { header: '地址', key: 'address', width: 28 },
    { header: '备注', key: 'remark', width: 18 },
    { header: '设备ID', key: 'device_id', width: 18 },
    { header: '经度', key: 'longitude', width: 14 },
    { header: '纬度', key: 'latitude', width: 14 },
    { header: '准确度', key: 'accuracy', width: 12 },
    { header: '是否合法', key: 'is_legal', width: 12 },
    { header: '业务ID', key: 'biz_id', width: 24 },
    { header: '记录ID', key: 'record_id', width: 24 }
  ];
  ws.addRows(source.rows);
  styleHeader(ws);
  ws.autoFilter = {
    from: { row: 1, column: 1 },
    to: { row: Math.max(1, ws.rowCount), column: ws.columnCount }
  };

  return Buffer.from(await wb.xlsx.writeBuffer());
}

export function sourcePreviewPayload(source, limit = 200) {
  return {
    month: source.month,
    root_dept_id: source.root_dept_id,
    include_children: source.include_children,
    dept_count: source.dept_count,
    user_count: source.user_count,
    record_count: source.record_count,
    generated_at: source.generated_at,
    columns: source.monthly.columns,
    rows: source.monthly.rows.slice(0, limit),
    preview_limit: limit
  };
}

function appendMonthlySheet(wb, source) {
  const ws = wb.addWorksheet('月度数据', { views: [{ state: 'frozen', ySplit: 4, xSplit: 6 }] });
  const ctx = sourceMonthContext(source.month);
  const fixedTop = monthlyFixedTopHeaders();
  const fixedSub = monthlyFixedSubHeaders();

  ws.addRow([`月度汇总 统计日期：${ctx.firstDate} 至 ${ctx.lastDate}`]);
  ws.addRow([`报表生成时间：${formatDateTime(new Date()).slice(0, 16)}`]);
  ws.addRow(fixedTop.concat(Array.from({ length: ctx.days }, () => '考勤结果')));
  ws.addRow(fixedSub.map(column => column.label).concat(ctx.dayHeaders));
  for (const row of source.monthly.rows) {
    ws.addRow(fixedSub.map(column => row[column.key] ?? '').concat(ctx.daysArray.map(day => row[`day_${day}`] ?? '')));
  }

  ws.columns = fixedSub.map((column, index) => ({
    width: index === 0 ? 20 : index < 6 ? 16 : 12
  })).concat(ctx.daysArray.map(() => ({ width: 22 })));
  styleHeader(ws, 3);
  styleHeader(ws, 4);
  ws.eachRow((row, rowNumber) => {
    if (rowNumber >= 5) row.alignment = { vertical: 'top', wrapText: true };
  });
}

function appendOriginalSheet(wb, source) {
  const ws = wb.addWorksheet('原始数据', { views: [{ state: 'frozen', ySplit: 3, xSplit: 6 }] });
  const ctx = sourceMonthContext(source.month);
  ws.addRow([`原始记录 统计日期：${ctx.firstDate} 至 ${ctx.lastDate}`]);
  ws.addRow([`报表生成时间：${formatDateTime(new Date()).slice(0, 16)}`]);
  ws.addRow([
    '姓名', '考勤组', '部门', '工号', '职位', 'UserId', '考勤日期', '考勤时间', '打卡时间', '打卡结果',
    '打卡地址', '打卡备注', '异常打卡原因', '打卡图片1', '打卡图片2', '打卡设备', '管理员修改备注',
    '管理员修改备注图片1', '管理员修改备注图片2', '管理员修改备注图片3'
  ]);
  for (const row of source.rows) {
    ws.addRow([
      row.user_name,
      row.attendance_group,
      row.department,
      row.job_number,
      row.position,
      row.user_id,
      formatOriginalAttendanceDate(row.work_date || row.date),
      row.plan_check_time,
      row.check_time,
      joinResultText(row),
      row.address,
      row.remark,
      row.error_reason,
      '',
      '',
      row.device_id,
      '',
      '',
      '',
      ''
    ]);
  }
  ws.columns = [
    18, 16, 24, 12, 16, 24, 18, 21, 21, 16, 36, 20, 18, 12, 12, 20, 18, 18, 18, 18
  ].map(width => ({ width }));
  styleHeader(ws, 3);
  ws.eachRow((row, rowNumber) => {
    if (rowNumber >= 4) row.alignment = { vertical: 'top', wrapText: true };
  });
}

function buildMonthlySheet(ctx, users, rows) {
  const rowsByUserDay = new Map();
  for (const row of rows) {
    const key = `${row.user_id}|${row.work_date || row.date}`;
    if (!rowsByUserDay.has(key)) rowsByUserDay.set(key, []);
    rowsByUserDay.get(key).push(row);
  }

  const columns = monthlyFixedSubHeaders()
    .map(column => ({ key: column.key, label: column.label }))
    .concat(ctx.daysArray.map(day => ({ key: `day_${day}`, label: `${day}${ctx.weekdayByDay[day] ? ` ${ctx.weekdayByDay[day]}` : ''}` })));

  const matrixRows = users.map(user => {
    const userRows = rows.filter(row => row.user_id === user.user_id);
    const out = {
      name: user.name,
      attendance_group: mostFrequent(userRows.map(row => row.attendance_group)),
      department: user.department || user.dept_ids.join(','),
      job_number: user.job_number || '',
      position: user.position || '',
      user_id: user.user_id,
      attendance_days: 0,
      holiday_overtime: '',
      rest_overtime: '',
      workday_overtime: '',
      rest_days: 0,
      work_minutes: 0,
      late_count: 0,
      late_minutes: 0,
      serious_late_count: '',
      serious_late_minutes: '',
      absenteeism_late_count: '',
      early_count: 0,
      early_minutes: 0,
      on_missing_count: 0,
      off_missing_count: 0,
      absenteeism_days: 0,
      business_trip_hours: '',
      outing_hours: '',
      personal_leave_days: '',
      comp_leave_days: '',
      sick_leave_days: '',
      annual_leave_days: '',
      maternity_leave_days: '',
      paternity_leave_days: '',
      marriage_leave_days: '',
      bereavement_leave_days: '',
      nursing_leave_hours: '',
      overtime_total_hours: '',
      workday_to_comp: '',
      rest_to_comp: '',
      holiday_to_comp: '',
      hangzhou: '',
      after_sales: '',
      ningbo: '',
      customer_mid: '',
      customer_late: '',
      customer_day: ''
    };

    for (const day of ctx.daysArray) {
      const date = `${ctx.month}-${String(day).padStart(2, '0')}`;
      const dayRows = rowsByUserDay.get(`${user.user_id}|${date}`) || [];
      const cell = buildMonthlyDayCell(dayRows);
      out[`day_${day}`] = cell.text;
      if (cell.hasAttendance) out.attendance_days += 1;
      else out.rest_days += 1;
      out.work_minutes += cell.workMinutes;
      out.late_count += cell.lateCount;
      out.late_minutes += cell.lateMinutes;
      out.early_count += cell.earlyCount;
      out.early_minutes += cell.earlyMinutes;
      out.on_missing_count += cell.onMissing ? 1 : 0;
      out.off_missing_count += cell.offMissing ? 1 : 0;
      out.absenteeism_days += cell.absenteeism ? 1 : 0;
    }

    out.work_minutes = Math.round(out.work_minutes);
    return out;
  });

  return { columns, rows: matrixRows };
}

function buildMonthlyDayCell(dayRows) {
  if (!dayRows.length) {
    return {
      text: '休息\n(-)',
      hasAttendance: false,
      workMinutes: 0,
      lateCount: 0,
      lateMinutes: 0,
      earlyCount: 0,
      earlyMinutes: 0,
      onMissing: false,
      offMissing: false,
      absenteeism: false
    };
  }

  const sorted = [...dayRows].sort((a, b) => toTimeValue(a.check_time) - toTimeValue(b.check_time));
  const onRows = sorted.filter(isOnDuty);
  const offRows = sorted.filter(isOffDuty);
  const on = onRows[0] || (sorted.length === 1 && !isOffDuty(sorted[0]) ? sorted[0] : null);
  const off = offRows[offRows.length - 1] || (sorted.length > 1 ? sorted[sorted.length - 1] : null);
  const onTime = on ? timePart(on.check_time) : '-';
  const offTime = off ? timePart(off.check_time) : '-';
  const segments = [];
  const group = mostFrequent(sorted.map(row => row.attendance_group)) || '钉钉';

  let lateCount = 0;
  let lateMinutes = 0;
  let earlyCount = 0;
  let earlyMinutes = 0;
  const absenteeism = sorted.some(row => isAbsenteeism(row.time_result));
  if (absenteeism) segments.push('旷工');

  for (const row of sorted) {
    const duty = isOnDuty(row) ? '上班' : isOffDuty(row) ? '下班' : '';
    if (isLate(row.time_result)) {
      lateCount += 1;
      const mins = Math.max(diffMinutes(row.plan_check_time, row.check_time), 0);
      lateMinutes += mins;
      segments.push(`${duty || '上班'}迟到${mins}分钟`);
    }
    if (isEarly(row.time_result)) {
      earlyCount += 1;
      const mins = Math.max(-diffMinutes(row.plan_check_time, row.check_time), 0);
      earlyMinutes += mins;
      segments.push(`${duty || '下班'}早退${mins}分钟`);
    }
    if (isOutside(row.location_result)) segments.push(`${duty || '打卡'}外勤`);
    if (isMissing(row.time_result)) segments.push(`${duty || '打卡'}缺卡`);
  }

  const onMissing = onTime === '-';
  const offMissing = offTime === '-';
  if (onMissing) segments.push('上班缺卡');
  if (offMissing) segments.push('下班缺卡');
  if (!segments.length) segments.push('正常');

  const workMinutes = on && off ? Math.max(0, toTimeValue(off.check_time) - toTimeValue(on.check_time)) / 60000 : 0;
  return {
    text: `${group}:${unique(segments).join(',')}\n(${onTime},${offTime})`,
    hasAttendance: Boolean(on || off),
    workMinutes,
    lateCount,
    lateMinutes,
    earlyCount,
    earlyMinutes,
    onMissing,
    offMissing,
    absenteeism
  };
}

function monthlyFixedTopHeaders() {
  return [
    '姓名', '考勤组', '部门', '工号', '职位', 'UserId', '出勤天数', '节假日加班', '休息日加班', '工作日加班',
    '休息天数', '工作时长', '迟到次数', '迟到时长', '严重迟到次数', '严重迟到时长', '旷工迟到次数',
    '早退次数', '早退时长', '上班缺卡次数', '下班缺卡次数', '旷工天数', '出差时长', '外出时长',
    '请假', '', '', '', '', '', '', '', '', '加班总时长', '加班时长（转调休）', '', '', '出勤班次', '', '', '', '', ''
  ];
}

function monthlyFixedSubHeaders() {
  return [
    { key: 'name', label: '姓名' },
    { key: 'attendance_group', label: '考勤组' },
    { key: 'department', label: '部门' },
    { key: 'job_number', label: '工号' },
    { key: 'position', label: '职位' },
    { key: 'user_id', label: 'UserId' },
    { key: 'attendance_days', label: '出勤天数' },
    { key: 'holiday_overtime', label: '节假日加班' },
    { key: 'rest_overtime', label: '休息日加班' },
    { key: 'workday_overtime', label: '工作日加班' },
    { key: 'rest_days', label: '休息天数' },
    { key: 'work_minutes', label: '工作时长' },
    { key: 'late_count', label: '迟到次数' },
    { key: 'late_minutes', label: '迟到时长' },
    { key: 'serious_late_count', label: '严重迟到次数' },
    { key: 'serious_late_minutes', label: '严重迟到时长' },
    { key: 'absenteeism_late_count', label: '旷工迟到次数' },
    { key: 'early_count', label: '早退次数' },
    { key: 'early_minutes', label: '早退时长' },
    { key: 'on_missing_count', label: '上班缺卡次数' },
    { key: 'off_missing_count', label: '下班缺卡次数' },
    { key: 'absenteeism_days', label: '旷工天数' },
    { key: 'business_trip_hours', label: '出差时长' },
    { key: 'outing_hours', label: '外出时长' },
    { key: 'personal_leave_days', label: '事假(天)' },
    { key: 'comp_leave_days', label: '调休(天)' },
    { key: 'sick_leave_days', label: '病假(天)' },
    { key: 'annual_leave_days', label: '年假(天)' },
    { key: 'maternity_leave_days', label: '产假(天)' },
    { key: 'paternity_leave_days', label: '陪产假(天)' },
    { key: 'marriage_leave_days', label: '婚假(天)' },
    { key: 'bereavement_leave_days', label: '丧假(天)' },
    { key: 'nursing_leave_hours', label: '哺乳假(小时)' },
    { key: 'overtime_total_hours', label: '加班总时长' },
    { key: 'workday_to_comp', label: '工作日（转调休）' },
    { key: 'rest_to_comp', label: '休息日（转调休）' },
    { key: 'holiday_to_comp', label: '节假日（转调休）' },
    { key: 'hangzhou', label: '杭州' },
    { key: 'after_sales', label: '售后部' },
    { key: 'ningbo', label: '宁波组' },
    { key: 'customer_mid', label: '客服部中班' },
    { key: 'customer_late', label: '客服部晚班' },
    { key: 'customer_day', label: '客服部白班' }
  ];
}

export async function loadDingTalkConfig() {
  const configFile = process.env.DINGTALK_CONFIG_FILE;
  let fileConfig = {};
  if (configFile) {
    fileConfig = JSON.parse(await fs.readFile(configFile, 'utf8'));
  }
  const appKey = process.env.DINGTALK_APP_KEY || fileConfig.appKey || fileConfig.clientId;
  const appSecret = process.env.DINGTALK_APP_SECRET || fileConfig.appSecret || fileConfig.clientSecret;
  if (!appKey || !appSecret) {
    throw new Error('钉钉未配置：请设置 DINGTALK_CONFIG_FILE，或设置 DINGTALK_APP_KEY / DINGTALK_APP_SECRET');
  }
  return {
    appKey,
    appSecret,
    defaultDeptId: process.env.DINGTALK_DEFAULT_DEPT_ID || fileConfig.deptId || 1,
    robotCode: process.env.DINGTALK_ROBOT_CODE || fileConfig.robotCode || '',
    adminDebugToken: process.env.DINGTALK_ADMIN_DEBUG_TOKEN || fileConfig.adminDebugToken || '',
    fulltimeAttendanceReviewerUserId: process.env.FULLTIME_ATTENDANCE_REVIEWER_USER_ID
      || fileConfig.fulltimeAttendanceReviewerUserId || ''
  };
}

export async function getDingTalkAccessToken(config) {
  const url = `${OAPI_BASE}/gettoken?appkey=${encodeURIComponent(config.appKey)}&appsecret=${encodeURIComponent(config.appSecret)}`;
  const data = await requestJson(url);
  if (data.errcode !== 0 || !data.access_token) {
    throw new Error(`钉钉 access_token 获取失败：${data.errmsg || data.errcode}`);
  }
  return data.access_token;
}

async function collectDepartmentIds(accessToken, rootDeptId) {
  const out = [];
  const queue = [rootDeptId];
  const seen = new Set();
  while (queue.length) {
    const deptId = queue.shift();
    if (seen.has(deptId)) continue;
    seen.add(deptId);
    out.push(deptId);
    const data = await requestJson(`${OAPI_BASE}/topapi/v2/department/listsubid?access_token=${accessToken}`, {
      method: 'POST',
      body: { dept_id: deptId }
    });
    if (data.errcode !== 0) throw new Error(`钉钉部门列表获取失败：${data.errmsg || data.errcode}`);
    for (const child of data.result?.dept_id_list || []) queue.push(Number(child));
  }
  return out;
}

async function collectUsers(accessToken, deptIds) {
  const usersById = new Map();
  for (const deptId of deptIds) {
    let cursor = 0;
    while (true) {
      const data = await requestJson(`${OAPI_BASE}/topapi/v2/user/list?access_token=${accessToken}`, {
        method: 'POST',
        body: { dept_id: deptId, cursor, size: 100 }
      });
      if (data.errcode !== 0) throw new Error(`钉钉部门人员获取失败：${data.errmsg || data.errcode}`);
      for (const user of data.result?.list || []) {
        const userId = user.userid || user.userId;
        if (!userId) continue;
        usersById.set(userId, {
          user_id: userId,
          name: user.name || userId,
          dept_ids: user.dept_id_list || [],
          department: (user.dept_id_list || []).join(','),
          job_number: user.job_number || user.jobNumber || '',
          position: user.title || user.position || ''
        });
      }
      if (!data.result?.has_more) break;
      cursor = data.result.next_cursor;
      if (cursor == null) break;
    }
  }
  return [...usersById.values()].sort((a, b) => String(a.name).localeCompare(String(b.name), 'zh-CN'));
}

async function collectAttendanceRecords(accessToken, users, month) {
  const rows = [];
  const ranges = monthRanges(month);
  const userChunks = chunk(users.map(user => user.user_id), MAX_USER_CHUNK);
  for (const userIds of userChunks) {
    for (const range of ranges) {
      const data = await requestJson(`${OAPI_BASE}/attendance/listRecord?access_token=${accessToken}`, {
        method: 'POST',
        body: {
          userIds,
          checkDateFrom: `${range.from} 00:00:00`,
          checkDateTo: `${range.to} 23:59:59`
        }
      });
      if (data.errcode !== 0) throw new Error(`钉钉打卡详情获取失败：${data.errmsg || data.errcode}`);
      rows.push(...(data.recordresult || []));
    }
  }
  return rows;
}

function normalizeAttendanceRecord(record, users) {
  const userId = record.userId || record.userid || record.user_id || '';
  const user = users.find(item => item.user_id === userId);
  const checkTime = formatDingTime(record.userCheckTime || record.baseCheckTime || record.checkTime);
  const workDate = formatDingDate(record.workDate || record.baseCheckTime || record.userCheckTime);
  const planCheckTime = formatDingTime(record.planCheckTime);
  return {
    user_name: record.userName || user?.name || userId,
    user_id: userId,
    date: workDate || (checkTime ? checkTime.slice(0, 10) : ''),
    check_time: checkTime,
    check_type: record.checkType || '',
    time_result: record.timeResult || '',
    location_result: record.locationResult || '',
    group_id: record.groupId || '',
    attendance_group: record.groupId ? `考勤组${record.groupId}` : '',
    class_id: record.classId || '',
    plan_check_time: planCheckTime,
    work_date: workDate,
    department: user?.department || '',
    job_number: user?.job_number || '',
    position: user?.position || '',
    base_ssid: record.baseSsid || '',
    base_mac_addr: record.baseMacAddr || '',
    address: [record.userAddress, record.baseAddress, record.baseSsid, record.baseMacAddr].filter(Boolean).join('\n'),
    remark: record.remark || record.procInstId || '',
    error_reason: record.errorReason || '',
    device_id: record.deviceId || '',
    longitude: record.userLongitude ?? '',
    latitude: record.userLatitude ?? '',
    accuracy: record.userAccuracy ?? '',
    is_legal: record.isLegal ?? '',
    biz_id: record.bizId || '',
    record_id: record.id || ''
  };
}

function normalizeUserName(value) {
  return String(value || '').replace(/\s+/g, '').trim();
}

function normalizedNameCandidates(value) {
  const normalized = normalizeUserName(value);
  if (!normalized) return [];
  const candidates = new Set([normalized]);
  for (const part of normalized.split(/[-—－]/)) {
    if (part) candidates.add(part);
  }
  return [...candidates];
}

async function requestJson(url, options = {}) {
  const init = { method: options.method || 'GET', headers: {} };
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
    throw new Error(`钉钉接口返回非JSON：HTTP ${res.status}`);
  }
  if (!res.ok) throw new Error(`钉钉接口HTTP失败：${res.status} ${data.errmsg || ''}`);
  return data;
}

function normalizeMonth(value) {
  const raw = String(value || '').trim();
  if (!/^\d{4}-\d{2}$/.test(raw)) throw new Error('请选择正确的月份，格式为 YYYY-MM');
  return raw;
}

function monthRanges(month) {
  const [year, monthNumber] = month.split('-').map(Number);
  const lastDay = new Date(year, monthNumber, 0).getDate();
  const ranges = [];
  for (let start = 1; start <= lastDay; start += MAX_DAY_CHUNK) {
    const end = Math.min(start + MAX_DAY_CHUNK - 1, lastDay);
    ranges.push({
      from: `${year}-${String(monthNumber).padStart(2, '0')}-${String(start).padStart(2, '0')}`,
      to: `${year}-${String(monthNumber).padStart(2, '0')}-${String(end).padStart(2, '0')}`
    });
  }
  return ranges;
}

function chunk(values, size) {
  const out = [];
  for (let i = 0; i < values.length; i += size) out.push(values.slice(i, i + size));
  return out;
}

function formatDingTime(value) {
  if (!value) return '';
  if (typeof value === 'number') return formatDateTime(new Date(value));
  const n = Number(value);
  if (Number.isFinite(n) && String(value).length >= 10) return formatDateTime(new Date(n));
  return String(value);
}

function formatDingDate(value) {
  const text = formatDingTime(value);
  return text ? text.slice(0, 10) : '';
}

function formatDateTime(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}:${String(date.getSeconds()).padStart(2, '0')}`;
}

function sourceMonthContext(month) {
  const [year, monthNumber] = month.split('-').map(Number);
  const days = new Date(year, monthNumber, 0).getDate();
  const daysArray = Array.from({ length: days }, (_, i) => i + 1);
  const weekdayByDay = {};
  const dayHeaders = daysArray.map(day => {
    const d = new Date(year, monthNumber - 1, day);
    const weekday = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()];
    weekdayByDay[day] = weekday;
    return d.getDay() === 0 || d.getDay() === 6 ? weekday : String(day);
  });
  return {
    month,
    year,
    monthNumber,
    days,
    daysArray,
    dayHeaders,
    weekdayByDay,
    firstDate: `${month}-01`,
    lastDate: `${month}-${String(days).padStart(2, '0')}`
  };
}

function formatOriginalAttendanceDate(dateText) {
  if (!dateText) return '';
  const date = new Date(`${dateText}T00:00:00`);
  if (Number.isNaN(date.getTime())) return dateText;
  const yy = String(date.getFullYear()).slice(2);
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  const weekday = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'][date.getDay()];
  return `${yy}-${mm}-${dd} ${weekday}`;
}

function joinResultText(row) {
  const parts = [];
  const timeText = mapTimeResult(row.time_result);
  const locationText = mapLocationResult(row.location_result);
  if (timeText) parts.push(timeText);
  if (locationText) parts.push(locationText);
  return parts.join('\n') || '正常';
}

function mapTimeResult(value) {
  const raw = String(value || '');
  if (!raw || /Normal|正常/i.test(raw)) return '正常';
  if (/Late|迟到/i.test(raw)) return '迟到';
  if (/Early|早退/i.test(raw)) return '早退';
  if (/Absenteeism|旷工/i.test(raw)) return '旷工';
  if (/NotSigned|缺卡/i.test(raw)) return '缺卡';
  return raw;
}

function mapLocationResult(value) {
  const raw = String(value || '');
  if (!raw || /Normal|正常/i.test(raw)) return '';
  if (/Outside|外勤|外勤打卡/i.test(raw)) return '外勤';
  return raw;
}

function isOnDuty(row) {
  return /OnDuty|上班/i.test(String(row.check_type || ''));
}

function isOffDuty(row) {
  return /OffDuty|下班/i.test(String(row.check_type || ''));
}

function isLate(value) {
  return /Late|迟到/i.test(String(value || ''));
}

function isEarly(value) {
  return /Early|早退/i.test(String(value || ''));
}

function isAbsenteeism(value) {
  return /Absenteeism|旷工/i.test(String(value || ''));
}

function isMissing(value) {
  return /NotSigned|缺卡/i.test(String(value || ''));
}

function isOutside(value) {
  return /Outside|外勤/i.test(String(value || ''));
}

function diffMinutes(plan, actual) {
  const p = toTimeValue(plan);
  const a = toTimeValue(actual);
  if (!p || !a) return 0;
  return Math.round((a - p) / 60000);
}

function toTimeValue(value) {
  if (!value) return 0;
  const date = new Date(String(value).replace(' ', 'T'));
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function timePart(value) {
  if (!value) return '';
  const match = String(value).match(/(\d{1,2}):(\d{2})(?::\d{2})?$/);
  return match ? `${match[1].padStart(2, '0')}:${match[2]}` : '';
}

function mostFrequent(values) {
  const counts = new Map();
  for (const value of values.filter(Boolean)) counts.set(value, (counts.get(value) || 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '';
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function styleHeader(ws, rowNumber = 1) {
  const row = ws.getRow(rowNumber);
  row.font = { bold: true, color: { argb: 'FFFFFFFF' } };
  row.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: 'FF2456D6' } };
  row.alignment = { vertical: 'middle' };
  row.eachCell(cell => {
    cell.border = {
      top: { style: 'thin', color: { argb: 'FFD9E0EA' } },
      left: { style: 'thin', color: { argb: 'FFD9E0EA' } },
      bottom: { style: 'thin', color: { argb: 'FFD9E0EA' } },
      right: { style: 'thin', color: { argb: 'FFD9E0EA' } }
    };
  });
}
