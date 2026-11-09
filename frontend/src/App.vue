<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, ApiError, setSession, type Session } from './api'
import Parttime from './Parttime.vue'
import Fulltime from './Fulltime.vue'
import HumanResources from './HumanResources.vue'
import PublicAttendance from './PublicAttendance.vue'
import Performance from './Performance.vue'

const session = ref<Session | null>(null)
const ready = ref(false)
const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const module = ref<'parttime' | 'fulltime' | 'hr' | 'performance'>('parttime')
const publicPath = window.location.pathname.match(/^\/(attendance-(?:notice|review|ack))\/([A-Za-z0-9_-]+)$/)

function updateSession(value: Session | null) {
  session.value = value
  setSession(value)
  module.value = value?.role === 'performance' || new URLSearchParams(window.location.search).get('module') === 'performance'
    ? 'performance' : 'parttime'
}

onMounted(async () => {
  if (publicPath) return
  try { updateSession(await api<Session>('/session')) }
  catch (err) { if (!(err instanceof ApiError && err.status === 401)) error.value = String(err) }
  finally { ready.value = true }
})

async function login() {
  busy.value = true
  error.value = ''
  try {
    updateSession(await api<Session>('/session', { method: 'POST', body: JSON.stringify({
      username: username.value, password: password.value,
    }) }))
    password.value = ''
  } catch (err) { error.value = (err as Error).message }
  finally { busy.value = false }
}

async function logout() {
  try { await api('/session', { method: 'DELETE' }); updateSession(null) }
  catch (err) { error.value = (err as Error).message }
}
</script>

<template>
  <PublicAttendance v-if="publicPath" :path="publicPath[1]" :token="publicPath[2]" />
  <div v-else-if="!ready" class="center" role="status">正在读取登录状态…</div>
  <main v-else-if="!session" class="center">
    <form class="panel login" @submit.prevent="login">
      <p class="eyebrow">汇才 HR</p>
      <h1>登录管理工作台</h1>
      <label>账号<input v-model="username" autocomplete="username" required></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" required></label>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
      <button :disabled="busy">{{ busy ? '登录中…' : '登录' }}</button>
    </form>
  </main>
  <div v-else class="shell">
    <aside>
      <p class="brand">汇才 HR</p>
      <template v-if="session.role === 'admin'">
        <button class="nav-link" :class="{ 'nav-active': module === 'hr' }" @click="module = 'hr'">人事主数据</button>
        <p class="nav-heading">考勤管理</p>
        <button class="nav-link" :class="{ 'nav-active': module === 'parttime' }" @click="module = 'parttime'">兼职考勤</button>
        <button class="nav-link" :class="{ 'nav-active': module === 'fulltime' }" @click="module = 'fulltime'">全职考勤</button>
      </template>
      <button class="nav-link" :class="{ 'nav-active': module === 'performance' }" @click="module = 'performance'">绩效管理</button>
    </aside>
    <div class="workspace">
      <header><span>人事管理工作台</span><div><span>{{ session.actor }}</span><button class="secondary" @click="logout">退出</button></div></header>
      <main class="content">
        <p v-if="error" class="error" role="alert">{{ error }}</p>
        <Parttime v-if="module === 'parttime'" />
        <Fulltime v-else-if="module === 'fulltime'" />
        <HumanResources v-else-if="module === 'hr'" />
        <Performance v-else />
      </main>
    </div>
  </div>
</template>
