<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, awaitJob } from './api'

interface Delivery { id: string; employee_name: string; delivery_type: string; status: string }
interface Feedback { id: string; employee_name: string; category: string; detail: string; status: string; review_token: string | null }
interface Mapping { employee_name: string; recipient_user_id: string }
const props = defineProps<{ monthId: string; revision: number; versionId: string; feedbacks: Feedback[] }>()
const endpoint = () => `/attendance/fulltime/months/${props.monthId}`
const deliveries = ref<Delivery[]>([])
const mappings = ref<Mapping[]>([])
const names = ref('')
const testUser = ref('')
const purpose = ref<'test' | 'formal' | 'revision'>('test')
const recipient = ref({ employee_name: '', recipient_user_id: '' })
const exclusions = ref('')
const previews = ref<string[]>([])
const busy = ref(false)
const error = ref('')

async function run(action: () => Promise<void>) {
  error.value = ''
  busy.value = true
  try { await action() } catch (err) { error.value = (err as Error).message }
  finally { busy.value = false }
}

async function readDeliveries() {
  deliveries.value = (await api<{items: Delivery[]}>(`${endpoint()}/deliveries`)).items
}

async function readRecipients() {
  const saved = await api<{items: Mapping[]; exclusions: {employee_name: string}[]}>(`${endpoint()}/recipients`)
  mappings.value = saved.items
  exclusions.value = saved.exclusions.map(item => item.employee_name).join('、')
}

function parseNames(text: string) { return text.split(/[、,，\n]+/).map(item => item.trim()).filter(Boolean) }

async function saveRecipient() {
  await run(async () => {
    await api(`${endpoint()}/recipients`, { method: 'PUT', body: JSON.stringify({ items: [recipient.value] }) })
    await readRecipients()
  })
}

async function saveExclusions() {
  await run(async () => { await api(`${endpoint()}/notification-exclusions`, { method: 'PUT',
    body: JSON.stringify({ items: parseNames(exclusions.value) }) }) })
}

async function notify() {
  if (purpose.value !== 'test' && !confirm('按所选版本和收件人提交正式通知？')) return
  await run(async () => {
    const result = await api<{job_id: string; preview_urls?: string[]}>(`${endpoint()}/notifications`, { method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify({
        version_id: props.versionId, expected_revision: props.revision, delivery_type: purpose.value,
        employee_names: parseNames(names.value), test_recipient_user_id: testUser.value || null,
      }) })
    previews.value = result.preview_urls ?? []
    await awaitJob(result.job_id, () => {})
    await readDeliveries()
  })
}

async function reconcile(id: string) {
  await run(async () => {
    await api(`/attendance/fulltime/deliveries/${id}/reconcile`, { method: 'POST' })
    await readDeliveries()
  })
}

onMounted(() => run(async () => { await Promise.all([readDeliveries(), readRecipients()]) }))
</script>

<template>
  <section class="panel">
    <h2>通知接收人与排除名单</h2>
    <form class="grid" @submit.prevent="saveRecipient">
      <label>考勤姓名<input v-model="recipient.employee_name" required></label>
      <label>钉钉用户 ID<input v-model="recipient.recipient_user_id" required></label>
      <div class="actions"><button :disabled="busy">保存接收人</button></div>
    </form>
    <p v-if="mappings.length" class="muted">已配置 {{ mappings.length }} 位接收人。</p>
    <form class="grid" @submit.prevent="saveExclusions">
      <label>本月排除人员<textarea v-model="exclusions" placeholder="用顿号分隔姓名"></textarea></label>
      <div class="actions"><button class="secondary" :disabled="busy">保存排除名单</button></div>
    </form>
  </section>
  <section class="panel">
    <h2>发送考勤通知</h2>
    <form @submit.prevent="notify">
      <div class="grid"><label>用途<select v-model="purpose"><option value="test">测试</option><option value="formal">正式</option><option value="revision">修订重发</option></select></label>
        <label>员工姓名<textarea v-model="names" placeholder="用顿号分隔姓名" required></textarea></label>
        <label v-if="purpose === 'test'">测试钉钉用户 ID<input v-model="testUser" placeholder="本地预览可留空"></label></div>
      <button :disabled="busy || !versionId">提交通知任务</button>
    </form>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <div v-if="previews.length" class="actions nav-tabs">
      <a v-for="(url, index) in previews" :key="url" class="button secondary" :href="url" target="_blank" rel="noopener noreferrer">查看测试预览 {{ index + 1 }}</a>
    </div>
    <div v-if="deliveries.length" class="table-wrap nav-tabs"><table>
      <thead><tr><th>员工</th><th>用途</th><th>投递状态</th><th>操作</th></tr></thead>
      <tbody><tr v-for="item in deliveries" :key="item.id"><td>{{ item.employee_name }}</td><td>{{ item.delivery_type }}</td>
        <td>{{ item.status }}</td><td><button v-if="item.status === 'accepted'" class="secondary" :disabled="busy" @click="reconcile(item.id)">查询送达状态</button></td>
      </tr></tbody>
    </table></div>
  </section>
  <section v-if="feedbacks.length" class="panel">
    <h2>员工反馈</h2>
    <div v-for="item in feedbacks" :key="item.id" class="history-row">
      <span>{{ item.employee_name }} · {{ item.category }} · {{ item.detail }} · {{ item.status }}</span>
      <a v-if="item.review_token" class="button secondary" :href="`/attendance-review/${item.review_token}`" target="_blank" rel="noopener noreferrer">复核</a>
    </div>
  </section>
</template>
