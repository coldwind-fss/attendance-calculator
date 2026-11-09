<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, awaitJob } from './api'

interface Department { id: string; name: string; parentId: string; managerNames: string[] }
interface Employee { id: string; name: string; departmentIds: string[]; title: string; rosterStatus: string }
interface Profile { employee_id: string; fields: Record<string, string>; revision: number }
interface Staffing { department_id: string; department_name: string; approved_headcount: number; current_headcount: number; remaining_headcount: number; status: string; revision: number }
type RecruitmentStatus = 'RECRUITING' | 'URGENT' | 'PENDING_APPROVAL' | 'CLOSED'
interface Recruitment { id: string; department_name: string; position_name: string; demand_type: string; status: RecruitmentStatus; revision: number }
interface RecruitmentSummary {
  open_positions: number; open_positions_mom: number; pending_approvals: number; stale_positions: number;
  capacity_warnings: Staffing[]; monthly_snapshots: { month: string; open_positions: number }[];
}
interface RecruitmentTodos {
  pending_approvals: { id: string; request_id: string; created_at: string }[];
  capacity_warnings: Staffing[];
  stale_requests: { id: string; position_name: string; department_name: string; created_at: string }[];
}

const departments = ref<Department[]>([])
const employees = ref<Employee[]>([])
const staffing = ref<Staffing[]>([])
const requests = ref<Recruitment[]>([])
const recruitmentSummary = ref<RecruitmentSummary>()
const recruitmentTodos = ref<RecruitmentTodos>()
const recruitmentCursor = ref<string | null>(null)
const recruitmentFilters = ref({ department_id: '', status: '', demand_type: '', month: '' })
let recruitmentQuery = ''
const profile = ref<Profile>()
const scope = ref('current')
const filter = ref('')
const search = ref('')
const tab = ref('roster')
const busy = ref(false)
const error = ref('')
const cursor = ref<string | null>(null)
const demandTypes = ['新增编制', '离职补缺', '优化替换', '其他']
const recruitmentStatuses: Record<RecruitmentStatus, string> = {
  RECRUITING: '招聘中', URGENT: '紧急补岗', PENDING_APPROVAL: '待审批', CLOSED: '已关闭',
}

async function run(action: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try { await action() } catch (err) { error.value = (err as Error).message }
  finally { busy.value = false }
}

async function readEmployees(append = false) {
  const query = new URLSearchParams({ scope: scope.value, q: search.value })
  if (filter.value) query.set('department_id', filter.value)
  if (append && cursor.value) query.set('cursor', cursor.value)
  const result = await api<{ items: Employee[]; next_cursor: string | null }>(`/hr/employees?${query}`)
  employees.value = append ? [...employees.value, ...result.items] : result.items
  cursor.value = result.next_cursor
}

async function refresh() {
  const organization = await api<{departments: Department[]}>(`/hr/organization?scope=${scope.value}`)
  if (scope.value === 'current') departments.value = organization.departments
  await readEmployees()
  staffing.value = (await api<{items: Staffing[]}>('/hr/staffing')).items
}

async function readRecruitment(append = false) {
  const query = new URLSearchParams(append ? recruitmentQuery : undefined)
  if (!append) {
    for (const [key, value] of Object.entries(recruitmentFilters.value)) {
      if (value) query.set(key, value)
    }
  }
  if (append && recruitmentCursor.value) query.set('cursor', recruitmentCursor.value)
  const result = await api<{items: Recruitment[]; next_cursor: string | null}>(`/hr/recruitment/requests?${query}`)
  if (!append) recruitmentQuery = query.toString()
  requests.value = append ? [...requests.value, ...result.items] : result.items
  recruitmentCursor.value = result.next_cursor
}

async function refreshRecruitment() {
  const [summary, todos] = await Promise.all([
    api<RecruitmentSummary>('/hr/recruitment/dashboard'),
    api<RecruitmentTodos>('/hr/recruitment/todos'),
    readRecruitment(),
  ])
  recruitmentSummary.value = summary
  recruitmentTodos.value = todos
}

function openRecruitment() {
  tab.value = 'recruitment'
  return run(refreshRecruitment)
}

async function sync() {
  await run(async () => {
    const { job_id } = await api<{job_id: string}>('/hr/organization-syncs', { method: 'POST', body: JSON.stringify({ scope: scope.value }) })
    await awaitJob(job_id, () => {})
    await refresh()
  })
}

async function edit(employee: Employee) {
  await run(async () => { profile.value = await api<Profile>(`/hr/employees/${encodeURIComponent(employee.id)}/profile`) })
}

async function saveProfile() {
  const item = profile.value
  if (!item) return
  await run(async () => { profile.value = await api<Profile>(`/hr/employees/${encodeURIComponent(item.employee_id)}/profile`, {
    method: 'PATCH', body: JSON.stringify({ fields: { nickname: item.fields.nickname ?? '', notes: item.fields.notes ?? '' }, expected_revision: item.revision }),
  }) })
}

async function saveStaffing(item: Staffing) {
  await run(async () => {
    const updated = await api<Staffing>(`/hr/staffing/${encodeURIComponent(item.department_id)}`, { method: 'PUT',
      body: JSON.stringify({ approved_headcount: item.approved_headcount, expected_revision: item.revision }) })
    Object.assign(item, updated)
  })
}

async function updateRecruitment(item: Recruitment, action: string) {
  await run(async () => {
    const updated = await api<Recruitment>(`/hr/recruitment/requests/${encodeURIComponent(item.id)}`, { method: 'PATCH',
      body: JSON.stringify({ action, demand_type: item.demand_type, expected_revision: item.revision }) })
    Object.assign(item, updated)
    await refreshRecruitment()
  })
}

onMounted(() => run(refresh))
</script>

<template>
  <h1>人事主数据</h1>
  <div class="actions nav-tabs">
    <button :class="{secondary: tab !== 'roster'}" @click="tab = 'roster'">人员名册</button>
    <button :class="{secondary: tab !== 'organization'}" @click="tab = 'organization'">组织架构</button>
    <button :class="{secondary: tab !== 'staffing'}" @click="tab = 'staffing'">编制管理</button>
    <button :class="{secondary: tab !== 'recruitment'}" :disabled="busy" @click="openRecruitment">招聘需求</button>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <section v-if="tab === 'roster'" class="panel">
    <form class="grid" @submit.prevent="run(() => readEmployees())">
      <label>名册范围<select v-model="scope" @change="run(refresh)"><option value="current">在职与兼职</option><option value="departed">离职</option></select></label>
      <label>部门<select v-model="filter" @change="run(() => readEmployees())"><option value="">全部</option>
        <option v-for="item in departments" :key="item.id" :value="item.id">{{ item.name }}</option></select></label>
      <label>搜索<input v-model="search" placeholder="姓名或岗位"></label>
      <div class="actions"><button :disabled="busy">查询</button><button type="button" class="secondary" :disabled="busy" @click="sync">同步钉钉</button></div>
    </form>
    <div class="table-wrap"><table><thead><tr><th>姓名</th><th>岗位</th><th>状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="item in employees" :key="item.id"><td>{{ item.name }}</td><td>{{ item.title }}</td><td>{{ item.rosterStatus }}</td>
        <td><button class="secondary" @click="edit(item)">档案</button></td></tr></tbody></table></div>
    <button v-if="cursor" class="secondary" :disabled="busy" @click="run(() => readEmployees(true))">加载更多</button>
    <form v-if="profile" class="grid profile" @submit.prevent="saveProfile">
      <label>花名<input v-model="profile.fields.nickname"></label><label>备注<input v-model="profile.fields.notes"></label>
      <div class="actions"><button :disabled="busy">保存档案</button><button type="button" class="secondary" @click="profile = undefined">关闭</button></div>
    </form>
  </section>
  <section v-else-if="tab === 'organization'" class="panel table-wrap">
    <table><thead><tr><th>部门</th><th>上级部门</th><th>负责人</th></tr></thead><tbody>
      <tr v-for="item in departments" :key="item.id"><td>{{ item.name }}</td><td>{{ departments.find(parent => parent.id === item.parentId)?.name ?? '—' }}</td>
        <td>{{ item.managerNames?.join('、') }}</td></tr>
    </tbody></table>
  </section>
  <section v-else-if="tab === 'staffing'" class="panel table-wrap">
    <table><thead><tr><th>部门</th><th>批准编制</th><th>在岗</th><th>剩余</th><th>状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="item in staffing" :key="item.department_id"><td>{{ item.department_name }}</td>
        <td><input v-model.number="item.approved_headcount" type="number" min="0" max="300" :aria-label="`${item.department_name} 批准编制`"></td>
        <td>{{ item.current_headcount }}</td><td>{{ item.remaining_headcount }}</td><td>{{ item.status }}</td>
        <td><button class="secondary" :disabled="busy" @click="saveStaffing(item)">保存</button></td></tr></tbody></table>
  </section>
  <section v-else class="panel">
    <div v-if="recruitmentSummary" class="grid" aria-label="招聘概览">
      <span>招聘中 {{ recruitmentSummary.open_positions }}</span>
      <span>较上月 {{ recruitmentSummary.open_positions_mom > 0 ? '+' : '' }}{{ recruitmentSummary.open_positions_mom }}</span>
      <span>待审批 {{ recruitmentSummary.pending_approvals }}</span>
      <span>编制预警 {{ recruitmentSummary.capacity_warnings.length }}</span>
      <span>超过 7 天未关闭 {{ recruitmentSummary.stale_positions }}</span>
    </div>
    <details v-if="recruitmentTodos">
      <summary>招聘待办</summary>
      <ul>
        <li v-for="item in recruitmentTodos.pending_approvals" :key="item.id">需求 {{ item.request_id }} 待审批 · {{ item.created_at.slice(0, 10) }}</li>
        <li v-for="item in recruitmentTodos.capacity_warnings" :key="item.department_id">{{ item.department_name }} 已超编，编制余额 {{ item.remaining_headcount }}</li>
        <li v-for="item in recruitmentTodos.stale_requests" :key="item.id">{{ item.department_name }} · {{ item.position_name }}超过 7 天未关闭</li>
      </ul>
      <p v-if="!recruitmentTodos.pending_approvals.length && !recruitmentTodos.capacity_warnings.length && !recruitmentTodos.stale_requests.length">暂无待办</p>
    </details>
    <details v-if="recruitmentSummary?.monthly_snapshots.length">
      <summary>月度统计</summary>
      <table><thead><tr><th>月份</th><th>在招需求</th></tr></thead><tbody>
        <tr v-for="item in recruitmentSummary.monthly_snapshots" :key="item.month"><td>{{ item.month }}</td><td>{{ item.open_positions }}</td></tr>
      </tbody></table>
    </details>
    <form class="grid" @submit.prevent="run(() => readRecruitment())">
      <label>部门<select v-model="recruitmentFilters.department_id"><option value="">全部</option>
        <option v-for="item in staffing" :key="item.department_id" :value="item.department_id">{{ item.department_name }}</option></select></label>
      <label>状态<select v-model="recruitmentFilters.status"><option value="">全部</option>
        <option v-for="(label, status) in recruitmentStatuses" :key="status" :value="status">{{ label }}</option></select></label>
      <label>需求类型<select v-model="recruitmentFilters.demand_type"><option value="">全部</option>
        <option v-for="demand in demandTypes" :key="demand">{{ demand }}</option></select></label>
      <label>创建月份<input v-model="recruitmentFilters.month" type="month"></label>
      <div class="actions"><button :disabled="busy">查询</button></div>
    </form>
    <div class="table-wrap"><table><thead><tr><th>岗位</th><th>部门</th><th>需求类型</th><th>状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="item in requests" :key="item.id"><td>{{ item.position_name }}</td><td>{{ item.department_name }}</td>
        <td><select v-model="item.demand_type" :aria-label="`${item.position_name} 需求类型`"><option v-for="demand in demandTypes" :key="demand">{{ demand }}</option></select></td><td>{{ recruitmentStatuses[item.status] }}</td>
        <td class="actions"><button class="secondary" :disabled="busy" @click="updateRecruitment(item, 'change_demand_type')">保存类型</button>
          <button class="secondary" :disabled="busy || item.status === 'CLOSED'" @click="updateRecruitment(item, 'close')">关闭需求</button></td></tr></tbody></table></div>
    <button v-if="recruitmentCursor" class="secondary" :disabled="busy" @click="run(() => readRecruitment(true))">加载更多</button>
  </section>
</template>
