import { readFile, writeFile } from 'node:fs/promises';
import { parseFulltimeOutputWorkbook } from './adapters/attendance/fulltimeCore.js';
import { getAiTableStatus, previewAiTableBackendRows, previewAiTableSources, queryAiTableOperationRows } from './adapters/attendance/dingtalkAiTableCore.js';
import { exportDingTalkAttendanceSourceXlsx, queryDingTalkAttendanceSource, sourcePreviewPayload } from './adapters/attendance/dingtalkCore.js';
import { queryRobotMessageStatus, sendRobotMarkdown } from './adapters/attendance/dingtalkBotCore.js';
import {
  exportAnalysisXlsx, parseAttendanceOutputWorkbook,
  parseChannelOutputTables, parseOperationWorkbook, parseSchedule, parseSettlementOutputWorkbook,
  parseTmallSchedule, settlementPolicy, summarizeAnalysis,
} from './adapters/attendance/attendanceCore.js';

let input = '';
for await (const chunk of process.stdin.setEncoding('utf8')) input += chunk;
const { operation, source, output, month, options, rows, backend } = JSON.parse(input);

function checkMonth(rows) {
  if (rows.some(row => !row.date.startsWith(`${month}-`))) {
    throw new Error('工作簿含有其他月份的数据，请检查所选工作表');
  }
}

async function parseAnalysis() {
  const schedule = await parseSchedule(await readFile(source.schedule.path), options.schedule_sheet);
  if (source.tmall) {
    schedule.push(...await parseTmallSchedule(await readFile(source.tmall.path), options.tmall_schedule_sheet));
  }
  if (!schedule.length) throw new Error('未从排班表中识别到主播或中控排班');
  checkMonth(schedule);

  const calculation = {
    timeCalculationMode: options.calculation_mode,
    warningDiffHours: options.warning_threshold_hours,
  };
  let operations = [];
  if (source.operation) {
    const buffer = await readFile(source.operation.path);
    operations = await parseOperationWorkbook(buffer);
    calculation.attendanceOutput = await parseAttendanceOutputWorkbook(buffer);
    calculation.settlementOutput = await parseSettlementOutputWorkbook(buffer);
    calculation.channelOutputTables = await parseChannelOutputTables(buffer);
  } else if (options.operation_source === 'dingtalk_ai_table') {
    operations = await queryAiTableOperationRows({ scheduleRows: schedule });
  }
  return { schedule, backend, operations, calculation, policy: settlementPolicy(schedule) };
}

try {
  let result;
  switch (operation) {
    case 'fulltime-preview': result = await parseFulltimeOutputWorkbook(await readFile(source.path)); break;
    case 'ai-status': result = await getAiTableStatus(); break;
    case 'ai-preview': result = await previewAiTableSources(options); break;
    case 'ai-backend-preview': result = await previewAiTableBackendRows(options); break;
    case 'attendance-source': {
      const attendance = await queryDingTalkAttendanceSource(options);
      await writeFile(output, await exportDingTalkAttendanceSourceXlsx(attendance));
      result = sourcePreviewPayload(attendance, options.limit);
      break;
    }
    case 'notification-send': result = await sendRobotMarkdown(options); break;
    case 'notification-status': result = await queryRobotMessageStatus(options.process_query_key); break;
    case 'parse-analysis': result = await parseAnalysis(); break;
    case 'render-analysis': {
      result = summarizeAnalysis(rows.schedule, rows.backend, rows.operations, rows.calculation, rows.calculated);
      await writeFile(output, await exportAnalysisXlsx(result));
      break;
    }
    default: throw new Error('Unsupported engine operation');
  }
  process.stdout.write(JSON.stringify({ result }));
} catch (error) {
  const message = error.message;
  const local = ['fulltime-preview', 'parse-analysis', 'render-analysis'].includes(operation);
  const fallback = local ? '无法解析所选工作簿，请检查格式和工作表' : '钉钉接口未返回结果，请检查配置或稍后重试';
  process.stdout.write(JSON.stringify({ error: /^(工作簿含有|未从)/.test(message) ? message : fallback }));
  process.exitCode = 1;
}
