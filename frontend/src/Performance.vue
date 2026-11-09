<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from './api'

interface Assessment {
  employee_id: string; employee_name: string; month: string; department: string; result: number | string | null;
  workflow: { label: string; admin_action: 'remind' | 'send_self_review' | 'view_archive' | null };
}
interface RosterMember { employee_id: string; employee_name: string; department: string }
interface Approval {
  approval_id: string; month: string; department: string; status: string; instance_id: string; last_error: string; can_retry: boolean;
}
const months = ref<string[]>([])
const month = ref(new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit' }).format())
const kind = ref<'kpi' | 'okr'>('kpi')
const rows = ref<Assessment[]>([])
const busy = ref(false)
const error = ref('')
const verificationEmployee = ref('')
const verificationType = ref<'kpi' | 'result'>('kpi')
const approvals = ref<Approval[]>()
const roster = ref<RosterMember[]>([])
const selectedEmployee = ref('')
const message = ref('')
const approvalStatuses: Record<string, string> = {
  waiting: '等待归档', pending: '待提交', submitting: '提交中', submitted: '已提交',
  blocked: '权限受限', failed: '提交失败', disabled: '通道关闭',
}

async function run(action: () => Promise<void>) {
  busy.value = true
  error.value = ''
  message.value = ''
  try { await action() } catch (err) { error.value = (err as Error).message }
  finally { busy.value = false }
}

async function refresh() {
  const base = `/performance/months/${month.value}`
  const [assessments, members] = await Promise.all([
    api<{items: Assessment[]}>(`${base}/assessments?assessment_type=${kind.value}`),
    api<{items: RosterMember[]}>(`${base}/roster`),
  ])
  rows.value = assessments.items
  roster.value = members.items
  selectedEmployee.value = ''
  verificationEmployee.value = ''
}

async function invite() {
  await run(async () => {
    await api(`/performance/months/${month.value}/invitations`, { method: 'POST', body: JSON.stringify({
      employee_id: selectedEmployee.value, assessment_type: kind.value,
    }) })
    message.value = '已提交邀请，请查看流程和投递状态。'
    await refresh()
  })
}

async function remind(row: Assessment, action = 'remind') {
  await run(async () => {
    await api(`/performance/months/${row.month}/reminders`, { method: 'POST', body: JSON.stringify({
      employee_id: row.employee_id, assessment_type: kind.value, action,
    }) })
    message.value = '已提交办理请求。'
    await refresh()
  })
}

function archiveUrl(row: Assessment) {
  const [year, number] = row.month.split('-')
  const query = new URLSearchParams({ empId: row.employee_id, month: `${year}年${Number(number)}月`, type: 'result' })
  return `/hrms/performance/${kind.value === 'okr' ? 'okr-archive-view' : 'archive-view'}?${query}`
}

async function verifySignature() {
  await run(async () => {
    await api('/performance/signature-verifications', { method: 'POST', body: JSON.stringify({
      employee_id: verificationEmployee.value, month: month.value, document_type: verificationType.value,
    }) })
    message.value = '签署记录核验通过。'
  })
}

async function readApprovals() {
  approvals.value = (await api<{items: Approval[]}>('/performance/oa-approvals')).items
}

async function retryApproval(item: Approval) {
  await run(async () => {
    const result = await api<{success: boolean}>(`/performance/oa-approvals/${encodeURIComponent(item.approval_id)}/retries`, { method: 'POST' })
    await readApprovals()
    message.value = result.success ? '已提交部门审批。' : '本次重试未提交，请查看部门审批状态。'
  })
}

onMounted(() => run(async () => {
  months.value = (await api<{items: string[]}>('/performance/months')).items
  month.value = months.value[0] ?? month.value
  await refresh()
}))
</script>

<template>
  <div class="section-head"><div><h1>绩效管理</h1><p class="muted">按月查看 KPI 和 OKR 的目标、评分及签署归档进度。</p></div>
    <div class="actions"><a class="button secondary" href="/hrms/performance/okr-admin" target="_blank" rel="noopener noreferrer">OKR 流程办理</a></div>
  </div>
  <p v-if="error" class="error" role="alert">{{ error }}</p>
  <p v-if="message" role="status">{{ message }}</p>
  <section class="panel">
    <div class="grid">
      <label>考核月份<input v-model="month" type="month" @change="run(refresh)"></label>
      <label>考核类型<select v-model="kind" @change="run(refresh)"><option value="kpi">KPI</option><option value="okr">OKR</option></select></label>
      <div class="actions"><button :disabled="busy" @click="run(refresh)">刷新</button></div>
    </div>
    <form class="grid" @submit.prevent="invite">
      <label>月度考核员工<select v-model="selectedEmployee" required><option value="">请选择</option>
        <option v-for="item in roster" :key="item.employee_id" :value="item.employee_id">{{ item.employee_name }} · {{ item.department }}</option>
      </select></label>
      <div class="actions"><button :disabled="busy || !selectedEmployee">邀请填写目标</button></div>
    </form>
    <div class="table-wrap"><table>
      <thead><tr><th>员工</th><th>部门</th><th>考核月份</th><th>流程状态</th><th>核定结果</th><th>操作</th></tr></thead>
      <tbody><tr v-for="item in rows" :key="item.employee_id">
        <td>{{ item.employee_name || item.employee_id }}</td><td>{{ item.department }}</td><td>{{ item.month }}</td>
        <td>{{ item.workflow.label }}</td><td>{{ item.result ?? '—' }}</td>
        <td class="actions">
          <a v-if="item.workflow.admin_action === 'view_archive'" class="button secondary" :href="archiveUrl(item)" target="_blank" rel="noopener noreferrer">查看归档</a>
          <button v-else-if="item.workflow.admin_action === 'send_self_review'" class="secondary" :disabled="busy" @click="remind(item, 'send_self_review')">发送自评</button>
          <button v-else-if="item.workflow.admin_action === 'remind'" class="secondary" :disabled="busy" @click="remind(item)">催办</button>
        </td>
      </tr></tbody>
    </table></div>
    <p v-if="!rows.length" class="muted">该月暂无可读取的考核记录。</p>
  </section>
  <section class="panel"><h2>归档与部门审批</h2>
    <form v-if="kind === 'kpi'" class="grid" @submit.prevent="verifySignature">
      <label>签署员工<select v-model="verificationEmployee" required><option value="">请选择</option>
        <option v-for="item in rows" :key="item.employee_id" :value="item.employee_id">{{ item.employee_name || item.employee_id }}</option>
      </select></label>
      <label>签署文件<select v-model="verificationType"><option value="kpi">KPI 目标</option><option value="result">绩效结果</option></select></label>
      <div class="actions"><button class="secondary" :disabled="busy || !verificationEmployee">核验签署</button></div>
    </form>
    <div class="actions"><button class="secondary" :disabled="busy" @click="run(readApprovals)">查看 OA 状态</button></div>
    <p v-if="approvals && !approvals.length" class="muted">暂无部门审批记录。</p>
    <div v-if="approvals?.length" class="table-wrap"><table>
      <thead><tr><th>考核月份</th><th>部门</th><th>状态</th><th>审批单号</th><th>最近结果</th><th>操作</th></tr></thead>
      <tbody><tr v-for="item in approvals" :key="item.approval_id">
        <td>{{ item.month }}</td><td>{{ item.department }}</td><td>{{ approvalStatuses[item.status] ?? item.status }}</td>
        <td>{{ item.instance_id || '—' }}</td><td>{{ item.last_error || '—' }}</td>
        <td><button v-if="item.can_retry" class="secondary" :disabled="busy" @click="retryApproval(item)">重试提交</button></td>
      </tr></tbody>
    </table></div>
  </section>
</template>
