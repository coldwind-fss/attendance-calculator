<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { publicApi } from './api'

interface Feedback { id: string; category: string; detail: string; status: string; review_note: string | null }
interface Notice {
  payload: { employeeName: string; workbook: { year: number; month: number };
    statistics: { columns: { key: string; label: string }[]; row: Record<string, unknown> };
    attendance: { days: { day: number; status: string }[] } };
  feedbacks: Feedback[]; acknowledged_at: string | null;
}

const props = defineProps<{ path: string; token: string }>()
const notice = ref<Notice>()
const review = ref<Feedback>()
const error = ref('')
const busy = ref(false)
const detail = ref('')
const category = ref('每日状态')
const day = ref<number | ''>('')
const requested = ref('')
const isReview = props.path === 'attendance-review'
const endpoint = `/attendance/${isReview ? 'reviews' : 'notices'}/${encodeURIComponent(props.token)}`
const statuses = ['待玖杉处理', '处理中', '待员工补充', '已处理']
const categories = ['每日状态', '请假/出差/外出', '补卡/迟到/缺卡', '补贴/全勤/餐补', '其他']

async function run(action: () => Promise<void>) {
  busy.value = true
  error.value = ''
  try { await action() }
  catch (err) { error.value = (err as Error).message }
  finally { busy.value = false }
}

async function read() {
  if (isReview) review.value = await publicApi<Feedback>(endpoint)
  else notice.value = await publicApi<Notice>(endpoint)
}

async function acknowledge() {
  await run(async () => { notice.value = await publicApi<Notice>(`${endpoint}/acknowledgments`, { method: 'POST' }) })
}

async function send() {
  await run(async () => {
    await publicApi(`${endpoint}/feedbacks`, { method: 'POST', body: JSON.stringify({
      category: category.value, detail: detail.value, day_items: day.value ? [{ day: day.value, requested_status: requested.value }] : [],
    }) })
    detail.value = ''
    await read()
  })
}

async function update() {
  if (!review.value) return
  await run(async () => {
    review.value = await publicApi<Feedback>(endpoint, { method: 'PATCH', body: JSON.stringify({
      status: review.value?.status, review_note: review.value?.review_note ?? '',
    }) })
  })
}

onMounted(() => run(read))
</script>

<template>
  <main class="content public-attendance">
    <h1>{{ isReview ? '考勤反馈复核' : '个人考勤结果' }}</h1>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="busy" role="status">处理中…</p>
    <template v-if="notice">
      <p>{{ notice.payload.workbook.year }} 年 {{ notice.payload.workbook.month }} 月 · {{ notice.payload.employeeName }}</p>
      <section class="panel">
        <h2>考勤统计</h2>
        <dl class="grid"><div v-for="column in notice.payload.statistics.columns" :key="column.key">
          <dt>{{ column.label }}</dt><dd>{{ notice.payload.statistics.row[column.key] }}</dd>
        </div></dl>
        <button :disabled="busy || !!notice.acknowledged_at" @click="acknowledge">{{ notice.acknowledged_at ? '已阅' : '确认已阅' }}</button>
      </section>
      <section class="panel">
        <h2>每日考勤</h2>
        <div class="table-wrap"><table><thead><tr><th>日期</th><th>考勤状态</th></tr></thead>
          <tbody><tr v-for="item in notice.payload.attendance.days" :key="item.day"><td>{{ item.day }} 日</td><td>{{ item.status }}</td></tr></tbody>
        </table></div>
      </section>
      <form class="panel" @submit.prevent="send">
        <h2>反馈问题</h2>
        <div class="grid"><label>类型<select v-model="category"><option v-for="item in categories" :key="item">{{ item }}</option></select></label>
          <label>日期（选填）<input v-model.number="day" type="number" min="1" max="31"></label>
          <label v-if="day">建议状态<input v-model="requested" maxlength="40"></label>
          <label>说明<textarea v-model="detail" required maxlength="3000"></textarea></label></div>
        <button :disabled="busy">提交反馈</button>
      </form>
      <section v-if="notice.feedbacks.length" class="panel"><h2>处理记录</h2>
        <div v-for="item in notice.feedbacks" :key="item.id" class="history-row"><span>{{ item.detail || item.category }}</span><span>{{ item.status }}</span></div>
      </section>
    </template>
    <form v-if="review" class="panel" @submit.prevent="update">
      <p>{{ review.category }} · {{ review.detail }}</p>
      <div class="grid"><label>状态<select v-model="review.status"><option v-for="item in statuses" :key="item">{{ item }}</option></select></label>
        <label>复核备注<textarea v-model="review.review_note" maxlength="3000"></textarea></label></div>
      <button :disabled="busy">保存处理结果</button>
    </form>
  </main>
</template>
