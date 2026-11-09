<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api, awaitJob, upload } from './api'
import FulltimeNotices from './FulltimeNotices.vue'

interface Version { id: string; version_number: number; status: string; label: string }
interface Correction { id: string; employee_name: string; reason: string; target_type: string }
interface Employee { name: string; entry_date: string; attendance_type: string; scheduled_days?: number | string }
interface Month {
  id: string; revision: number; current_version_id: string; published_version_id: string | null;
  versions: Version[]; corrections: Correction[];
  feedbacks: Array<{ id: string; employee_name: string; category: string; detail: string; status: string; review_token: string | null }>;
  version: { snapshot: { summary: Record<string, number>; statistics: {
    columns: Array<{ key: string; label: string }>; rows: Array<Record<string, string | number>>;
  } } } | null;
}

const month = ref(new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).format())
const data = ref<Month | null>(null)
const available = ref<{id: string; year: number; month: number}[]>([])
const busy = ref(false)
const error = ref('')
const progress = ref('')
const mode = ref<'workbook' | 'source_merge'>('workbook')
const workbook = ref<File>()
const monthly = ref<File>()
const raw = ref<File>()
const selectedVersion = ref('')
const correction = ref({ employee_name: '', target_type: 'day', day: 1, field_key: '', new_value: '', reason: '' })
const roster = ref<Employee[]>([])
const employee = ref<Employee>({ name: '', entry_date: '', attendance_type: '' })
const sourceId = ref('')
const endpoint = () => `/attendance/fulltime/months/FTM-${month.value.replace('-', '')}`
const pick = (event: Event) => (event.target as HTMLInputElement).files?.[0]
const idempotency = () => ({ 'Idempotency-Key': crypto.randomUUID() })

async function refresh(preserveSelection = false) {
  const selectedMonth = month.value
  const route = endpoint()
  const months = (await api<{items: typeof available.value}>('/attendance/fulltime/months')).items
  const id = `FTM-${selectedMonth.replace('-', '')}`
  const detail = months.some(item => item.id === id) ? await api<Month>(route) : null
  if (selectedMonth !== month.value) return
  available.value = months
  data.value = detail
  if (!preserveSelection || !detail?.versions.some(version => version.id === selectedVersion.value)) {
    selectedVersion.value = detail?.current_version_id ?? ''
  }
}

async function action(run: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try { await run(); await refresh() }
  catch (err) { error.value = (err as Error).message }
  finally { busy.value = false; progress.value = '' }
}

async function calculate(recalculate = false) {
  await action(async () => {
    const body: Record<string, string | number> = { run_type: recalculate ? 'recalculate' : mode.value,
      expected_revision: data.value?.revision ?? 0 }
    if (!recalculate && mode.value === 'workbook' && workbook.value) {
      body.workbook_id = await upload('fulltime', 'workbook', workbook.value)
    }
    if (!recalculate && mode.value === 'source_merge' && monthly.value && raw.value) {
      [body.monthly_file_id, body.raw_file_id] = await Promise.all([
        upload('fulltime', 'monthly', monthly.value), upload('fulltime', 'raw', raw.value),
      ])
    }
    const { job_id } = await api<{job_id: string}>(`${endpoint()}/runs`, {
      method: 'POST', headers: idempotency(), body: JSON.stringify(body),
    })
    await awaitJob(job_id, job => { progress.value = job.status === 'queued' ? '等待核算…' : '正在处理工作簿…' })
  })
}

async function publish() {
  if (!confirm('将所选版本设为已发布版本？')) return
  await action(async () => { await api(`${endpoint()}/publications`, { method: 'POST', headers: idempotency(),
    body: JSON.stringify({ version_id: selectedVersion.value, expected_revision: data.value?.revision }) }) })
}

async function addCorrection() {
  await action(async () => {
    const item = correction.value
    const { job_id } = await api<{job_id: string}>(`${endpoint()}/corrections`, { method: 'POST', body: JSON.stringify({ ...item,
      new_value: item.target_type === 'statistic' ? Number(item.new_value) : item.new_value,
      expected_revision: data.value?.revision,
    }) })
    await awaitJob(job_id, () => { progress.value = '正在应用修订…' })
    correction.value = { ...item, new_value: '', reason: '' }
  })
}

async function reverse(id: string) {
  await action(async () => {
    const { job_id } = await api<{job_id?: string}>(`/attendance/fulltime/corrections/${id}/reversals`, { method: 'POST',
      body: JSON.stringify({ expected_revision: data.value?.revision }) })
    if (job_id) await awaitJob(job_id, () => { progress.value = '正在重算…' })
  })
}

async function readRoster() {
  roster.value = (await api<{items: Employee[]}>('/attendance/fulltime/roster')).items
}

async function saveEmployee() {
  await action(async () => {
    roster.value = (await api<{items: Employee[]}>('/attendance/fulltime/roster', { method: 'PATCH',
      body: JSON.stringify({ items: [employee.value] }) })).items
    employee.value = { name: '', entry_date: '', attendance_type: '' }
  })
}

async function querySource() {
  await action(async () => {
    sourceId.value = (await api<{source_id: string}>('/attendance/fulltime/source-queries', { method: 'POST',
      body: JSON.stringify({ month: month.value }) })).source_id
  })
}

onMounted(() => action(readRoster))

watch(month, (value, _previous, onCleanup) => {
  if (!value) return
  const source = new EventSource(`/api/v1/events/attendance/fulltime/FTM-${value.replace('-', '')}`)
  const update = () => {
    if (busy.value) return
    void refresh(true).catch(err => { error.value = (err as Error).message })
  }
  source.addEventListener('ready', update)
  source.addEventListener('changed', update)
  onCleanup(() => source.close())
}, { immediate: true })
</script>

<template>
  <h1>全职考勤</h1>
  <p class="muted">导入考勤、核算草稿、记录修订与发布月度结果。</p>
  <form class="panel" @submit.prevent="calculate()">
    <h2>月度核算</h2>
    <fieldset :disabled="busy">
      <div class="grid">
        <label>月份<input v-model="month" type="month" required @change="action(async () => {})"></label>
        <label>已保存月份<select v-model="month" @change="action(async () => {})">
          <option v-if="!available.some(item => item.id === `FTM-${month.replace('-', '')}`)" :value="month">当前选择</option>
          <option v-for="item in available" :key="item.id" :value="`${item.year}-${String(item.month).padStart(2, '0')}`">{{ item.year }} 年 {{ item.month }} 月</option>
        </select></label>
        <label>导入方式<select v-model="mode"><option value="workbook">整合工作簿</option><option value="source_merge">合并源表</option></select></label>
        <label v-if="mode === 'workbook'">整合工作簿<input type="file" accept=".xlsx" required @change="workbook = pick($event)"></label>
        <template v-else><label>月度汇总<input type="file" accept=".xlsx" required @change="monthly = pick($event)"></label>
          <label>打卡原始记录<input type="file" accept=".xlsx" required @change="raw = pick($event)"></label></template>
      </div>
      <div class="actions"><button>导入并计算</button>
        <button type="button" class="secondary" :disabled="!month" @click="querySource">查询钉钉考勤源</button>
        <a v-if="sourceId" class="button secondary" :href="`/api/v1/attendance/fulltime/source-queries/${sourceId}/workbook`">下载钉钉源数据</a></div>
    </fieldset>
    <p v-if="progress" role="status">{{ progress }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
  </form>
  <details class="panel">
    <summary>全职名册 · {{ roster.length }} 人</summary>
    <form class="nav-tabs" @submit.prevent="saveEmployee">
      <div class="grid"><label>姓名<input v-model="employee.name" required></label>
        <label>入职日期<input v-model="employee.entry_date" type="date" required></label>
        <label>出勤类型<input v-model="employee.attendance_type" required placeholder="双休、单休等"></label>
        <label>应出勤天数<input v-model.number="employee.scheduled_days" type="number" min="0" step="0.01"></label></div>
      <button :disabled="busy">保存人员</button>
    </form>
    <div v-for="item in roster" :key="item.name" class="history-row"><span>{{ item.name }} · {{ item.entry_date }} · {{ item.attendance_type }}</span>
      <button class="secondary" @click="employee = { ...item }">编辑</button></div>
  </details>
  <template v-if="data">
    <section class="panel">
      <div class="section-head"><h2>版本与发布</h2><button class="secondary" :disabled="busy" @click="calculate(true)">应用修订并重算</button></div>
      <div class="grid"><label>版本<select v-model="selectedVersion">
        <option v-for="version in data.versions" :key="version.id" :value="version.id">
          v{{ version.version_number }} · {{ version.label }}{{ version.id === data.published_version_id ? '（已发布）' : '' }}
        </option>
      </select></label></div>
      <div class="actions">
        <button :disabled="busy || !selectedVersion" @click="publish">发布所选版本</button>
        <a v-if="selectedVersion" class="button secondary" :href="`/api/v1/attendance/fulltime/versions/${selectedVersion}/workbook`">下载所选版本</a>
      </div>
    </section>
    <section v-if="data.version?.snapshot.statistics" class="panel">
      <h2>当前版本统计</h2>
      <div class="table-wrap"><table>
        <thead><tr><th v-for="col in data.version.snapshot.statistics.columns" :key="col.key">{{ col.label }}</th></tr></thead>
        <tbody><tr v-for="(row, index) in data.version.snapshot.statistics.rows" :key="index">
          <td v-for="col in data.version.snapshot.statistics.columns" :key="col.key">{{ row[col.key] }}</td>
        </tr></tbody>
      </table></div>
    </section>
    <form class="panel" @submit.prevent="addCorrection">
      <h2>人工修订</h2>
      <fieldset :disabled="busy">
        <div class="grid">
          <label>员工姓名<input v-model="correction.employee_name" required></label>
          <label>修订类型<select v-model="correction.target_type"><option value="day">日级</option><option value="statistic">统计</option><option value="remark">备注</option></select></label>
          <label v-if="correction.target_type === 'day'">日期<input v-model.number="correction.day" type="number" min="1" max="31" required></label>
          <label v-if="correction.target_type === 'statistic'">统计字段<select v-model="correction.field_key" required>
            <option v-for="col in data.version?.snapshot.statistics.columns" :key="col.key" :value="col.key">{{ col.label }}</option>
          </select></label>
          <label>修订值<input v-model="correction.new_value" :type="correction.target_type === 'statistic' ? 'number' : 'text'" step="any" required></label>
          <label>原因<input v-model="correction.reason" required></label>
        </div>
        <button>记录修订</button>
      </fieldset>
      <div v-for="item in data.corrections" :key="item.id" class="history-row">
        <span>{{ item.employee_name }} · {{ item.reason }}</span><button type="button" class="secondary" :disabled="busy" @click="reverse(item.id)">撤销</button>
      </div>
    </form>
    <FulltimeNotices :key="data.id" :month-id="data.id" :revision="data.revision" :version-id="selectedVersion" :feedbacks="data.feedbacks" />
  </template>
</template>
