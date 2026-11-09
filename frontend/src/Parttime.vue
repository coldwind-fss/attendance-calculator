<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, awaitJob, upload } from './api'

interface AnalysisReference { analysis_id: string; month_id: string; created_at: string }
interface Analysis extends AnalysisReference {
  result: {
    stats: Record<string, number | string>
    schedule: Array<{ id: string; date: string; person: string; role: string; store_name: string;
      planned_hours: number; backend_hours: number; final_hours: number; warning_level: string }>
    systemPayroll: Array<Record<string, string | number>>
    systemSettlement: Array<Record<string, string | number>>
    anomalies: Array<{ type: string; date: string; store_name: string; person: string; detail: string }>
  }
}

const props = defineProps<{ payroll: boolean }>()
const month = ref(new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).format())
const schedule = ref<{id: string; sheets: string[]}>()
const tmall = ref<{id: string; sheets: string[]}>()
const operation = ref<File>()
const backend = ref<File[]>([])
const stores = ref<(string | null)[]>([])
const operationSource = ref('upload')
const sourcePreview = ref<unknown>()
const view = ref<'schedule' | 'settlement' | 'payroll' | 'anomalies'>('schedule')
const scheduleSheet = ref('')
const tmallSheet = ref('')
const mode = ref('payroll_protection')
const threshold = ref(15)
const active = ref<Analysis | null>(null)
const history = ref<AnalysisReference[]>([])
const historyCursor = ref<string | null>(null)
const busy = ref(false)
const progress = ref('')
const error = ref('')

watch(() => props.payroll, payroll => {
  view.value = payroll ? 'payroll' : 'schedule'
  if (payroll && !active.value && history.value.length) void openAnalysis(history.value[0].analysis_id)
})

function selected(event: Event) { return Array.from((event.target as HTMLInputElement).files ?? []) }
function selectBackends(event: Event) {
  backend.value = selected(event)
  stores.value = backend.value.map(() => null)
}

async function selectSchedule(event: Event, sourceType: 'schedule' | 'tmall_schedule') {
  const input = event.target as HTMLInputElement
  const target = sourceType === 'schedule' ? schedule : tmall
  const sheet = sourceType === 'schedule' ? scheduleSheet : tmallSheet
  target.value = undefined
  sheet.value = ''
  const file = input.files?.[0]
  if (!file) return
  busy.value = true
  error.value = ''
  progress.value = '读取排班工作表…'
  try {
    const id = await upload('parttime', sourceType, file)
    const { sheets } = await api<{sheets: string[]}>('/attendance/parttime/schedule-inspections', {
      method: 'POST', body: JSON.stringify({ file_id: id, source_type: sourceType }),
    })
    target.value = { id, sheets }
  } catch (err) {
    input.value = ''
    error.value = (err as Error).message
  } finally {
    busy.value = false
    progress.value = ''
  }
}

async function preview() {
  busy.value = true
  error.value = ''
  try { sourcePreview.value = await api('/attendance/parttime/source-queries', { method: 'POST',
    body: JSON.stringify({ source: 'ai-preview', month: month.value, limit: 10 }) }) }
  catch (err) { error.value = (err as Error).message }
  finally { busy.value = false }
}

async function refresh(append = false) {
  const selectedMonth = month.value
  if (!selectedMonth) return
  const query = new URLSearchParams()
  const cursor = append ? historyCursor.value : null
  if (cursor) query.set('cursor', cursor)
  try {
    const page = await api<{items: AnalysisReference[]; next_cursor: string | null}>(`/attendance/parttime/months/${selectedMonth}/analyses?${query}`)
    if (month.value !== selectedMonth || (append && historyCursor.value !== cursor)) return
    history.value = append ? [...history.value, ...page.items] : page.items
    historyCursor.value = page.next_cursor
  } catch (err) { if (month.value === selectedMonth) error.value = (err as Error).message }
}

function changeMonth() {
  active.value = null
  history.value = []
  historyCursor.value = null
  sourcePreview.value = undefined
  error.value = ''
  return refresh()
}

async function openAnalysis(id: string) {
  const selectedMonth = month.value
  try {
    const result = await api<Analysis>(`/attendance/parttime/analyses/${id}`)
    if (month.value === selectedMonth && result.month_id === selectedMonth) active.value = result
  } catch (err) { if (month.value === selectedMonth) error.value = (err as Error).message }
}

async function run() {
  if (!schedule.value || !backend.value.length) return
  busy.value = true
  progress.value = '上传工作簿…'
  error.value = ''
  try {
    const [backendIds, operationId] = await Promise.all([
      Promise.all(backend.value.map(file => upload('parttime', 'backend', file))),
      operationSource.value === 'upload' && operation.value ? upload('parttime', 'operation', operation.value) : undefined,
    ])
    const { job_id } = await api<{ job_id: string }>(`/attendance/parttime/months/${month.value}/analyses`, {
      method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({
        schedule_file_id: schedule.value.id, schedule_sheet: scheduleSheet.value || null,
        tmall_schedule_file_id: tmall.value?.id, tmall_schedule_sheet: tmallSheet.value || null,
        backend_file_ids: backendIds, backend_stores: stores.value, operation_file_id: operationId,
        operation_source: operationSource.value,
        calculation_mode: mode.value, warning_threshold_hours: threshold.value / 60,
      }),
    })
    const result = await awaitJob(job_id, job => { progress.value = job.status === 'queued' ? '等待计算…' : '正在核算…' })
    await openAnalysis(result.analysis_id)
    await refresh()
  } catch (err) { error.value = (err as Error).message }
  finally { busy.value = false; progress.value = '' }
}

onMounted(refresh)
</script>

<template>
  <h1>{{ payroll ? '薪资计算' : '兼职考勤' }}</h1>
  <p class="muted">{{ payroll ? '根据兼职考勤工时计算应发、个税和实发工资。' : '按月核算直播排班、后台场次和经营数据。' }}</p>
  <form class="panel" @submit.prevent="run">
    <h2>新建核算</h2>
    <fieldset :disabled="busy">
      <div class="grid">
        <label>核算月份<input v-model="month" type="month" required @change="changeMonth"></label>
        <label>核算口径<select v-model="mode"><option value="payroll_protection">工资保护</option><option value="backend_allocation">后台分配</option></select></label>
        <label>差异阈值（分钟）<input v-model.number="threshold" type="number" min="0" required></label>
        <label>排班 Excel<input type="file" accept=".xlsx" required @change="selectSchedule($event, 'schedule')"></label>
        <label>排班工作表<select v-model="scheduleSheet" :disabled="!schedule"><option value="">默认第一张</option>
          <option v-for="name in schedule?.sheets" :key="name" :value="name">{{ name }}</option></select></label>
        <label>后台场次 Excel<input type="file" accept=".xlsx" multiple required @change="selectBackends"></label>
        <label>天猫排班 Excel<input type="file" accept=".xlsx" @change="selectSchedule($event, 'tmall_schedule')"></label>
        <label>天猫工作表<select v-model="tmallSheet" :disabled="!tmall"><option value="">默认第一张</option>
          <option v-for="name in tmall?.sheets" :key="name" :value="name">{{ name }}</option></select></label>
        <label>经营来源<select v-model="operationSource"><option value="upload">上传 Excel</option><option value="dingtalk_ai_table">钉钉 AI 表</option></select></label>
        <label v-if="operationSource === 'upload'">经营表 Excel<input type="file" accept=".xlsx" @change="operation = selected($event)[0]"></label>
        <label v-for="(file, index) in backend" :key="index">{{ file.name }} · 门店
          <select v-model="stores[index]"><option :value="null">按文件名识别</option><option value="official">官旗</option>
            <option value="sports">运动</option><option value="tmall">天猫</option></select></label>
      </div>
      <div class="actions"><button>开始核算</button><button v-if="operationSource === 'dingtalk_ai_table'" class="secondary" type="button" @click="preview">预览钉钉 AI 表</button></div>
    </fieldset>
    <p v-if="progress" role="status">{{ progress }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <details v-if="sourcePreview" class="nav-tabs"><summary>来源预览</summary><pre>{{ JSON.stringify(sourcePreview, null, 2) }}</pre></details>
  </form>
  <section v-if="active" class="panel">
    <div class="section-head"><h2>{{ active.month_id }} 核算结果</h2>
      <a class="button" :href="`/api/v1/attendance/parttime/analyses/${active.analysis_id}/workbook`">下载 Excel</a>
    </div>
    <dl class="metrics">
      <div><dt>核算人数</dt><dd>{{ active.result.stats.people_count }}</dd></div>
      <div><dt>核算工时</dt><dd>{{ active.result.stats.actual_person_hours }}</dd></div>
      <div><dt>工资合计</dt><dd>{{ active.result.stats.payroll_amount }}</dd></div>
      <div><dt>异常数量</dt><dd>{{ active.result.stats.anomaly_count }}</dd></div>
    </dl>
    <div class="actions nav-tabs">
      <button :class="{secondary: view !== 'schedule'}" @click="view = 'schedule'">核算明细</button>
      <button :class="{secondary: view !== 'settlement'}" @click="view = 'settlement'">时间结算</button>
      <button :class="{secondary: view !== 'payroll'}" @click="view = 'payroll'">工资结算</button>
      <button :class="{secondary: view !== 'anomalies'}" @click="view = 'anomalies'">异常</button>
    </div>
    <div v-if="view === 'schedule'" class="table-wrap"><table>
      <thead><tr><th>日期</th><th>姓名</th><th>岗位</th><th>门店</th><th>排班时长</th><th>后台时长</th><th>计入时长</th><th>异常</th></tr></thead>
      <tbody><tr v-for="row in active.result.schedule" :key="row.id">
        <td>{{ row.date }}</td><td>{{ row.person }}</td><td>{{ row.role }}</td><td>{{ row.store_name }}</td>
        <td>{{ row.planned_hours }}</td><td>{{ row.backend_hours }}</td><td>{{ row.final_hours }}</td><td>{{ row.warning_level }}</td>
      </tr></tbody>
    </table></div>
    <div v-else-if="view === 'anomalies'" class="table-wrap"><table>
      <thead><tr><th>异常类型</th><th>日期</th><th>门店</th><th>人员</th><th>说明</th></tr></thead>
      <tbody><tr v-for="(row, index) in active.result.anomalies" :key="index"><td>{{ row.type }}</td><td>{{ row.date }}</td>
        <td>{{ row.store_name }}</td><td>{{ row.person }}</td><td>{{ row.detail }}</td></tr></tbody>
    </table></div>
    <div v-else class="table-wrap"><table>
      <thead><tr><th>岗位</th><th>花名</th><th>姓名</th><th>合计时长</th>
        <template v-if="view === 'payroll'"><th>合计应发</th><th>个税</th><th>实发</th></template><th>备注</th></tr></thead>
      <tbody><tr v-for="(row, index) in (view === 'payroll' ? active.result.systemPayroll : active.result.systemSettlement)" :key="index">
        <td>{{ row.role }}</td><td>{{ row.alias }}</td><td>{{ row.name }}</td><td>{{ row.total_hours }}</td>
        <template v-if="view === 'payroll'"><td>{{ row.total_amount }}</td><td>{{ row.total_tax }}</td><td>{{ row.total_net }}</td></template><td>{{ row.remark }}</td>
      </tr></tbody>
    </table></div>
  </section>
  <section class="panel">
    <h2>{{ month }} 历史核算</h2>
    <p v-if="!history.length" class="muted">该月暂无核算记录。</p>
    <div v-for="entry in history" :key="entry.analysis_id" class="history-row">
      <span>{{ new Date(entry.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }}</span>
      <button class="secondary" @click="openAnalysis(entry.analysis_id)">查看结果</button>
    </div>
    <button v-if="historyCursor" class="secondary" @click="refresh(true)">加载更多</button>
  </section>
</template>
