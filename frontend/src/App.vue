<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, setSession, type Session } from './api'
import Parttime from './Parttime.vue'
import Fulltime from './Fulltime.vue'
import PublicAttendance from './PublicAttendance.vue'
import Performance from './Performance.vue'

const ready = ref(false)
const error = ref('')
const requestedModule = new URLSearchParams(window.location.search).get('module')
const module = ref<'parttime' | 'fulltime' | 'payroll' | 'performance'>(
  requestedModule === 'performance' ? 'performance' : 'parttime',
)
const publicPath = window.location.pathname.match(/^\/(attendance-(?:notice|review|ack))\/([A-Za-z0-9_-]+)$/)

async function openDemo() {
  error.value = ''
  try {
    setSession(await api<Session>('/session', { method: 'POST' }))
    ready.value = true
  } catch (err) {
    error.value = (err as Error).message
  }
}

onMounted(() => { if (!publicPath) void openDemo() })
</script>

<template>
  <PublicAttendance v-if="publicPath" :path="publicPath[1]" :token="publicPath[2]" />
  <main v-else-if="!ready" class="center">
    <p v-if="!error" role="status">正在打开演示…</p>
    <div v-else>
      <p class="error" role="alert">{{ error }}</p>
      <button @click="openDemo">重试</button>
    </div>
  </main>
  <div v-else class="shell">
    <aside>
      <p class="brand">考勤计算系统</p>
      <p class="nav-heading">考勤计算</p>
      <button class="nav-link" :class="{ 'nav-active': module === 'parttime' }" @click="module = 'parttime'">兼职考勤</button>
      <button class="nav-link" :class="{ 'nav-active': module === 'fulltime' }" @click="module = 'fulltime'">全职考勤</button>
      <button class="nav-link" :class="{ 'nav-active': module === 'payroll' }" @click="module = 'payroll'">薪资计算</button>
      <button class="nav-link" :class="{ 'nav-active': module === 'performance' }" @click="module = 'performance'">绩效</button>
    </aside>
    <div class="workspace">
      <header><span>考勤计算系统</span><span class="muted">功能演示</span></header>
      <main class="content">
        <div v-show="module === 'parttime' || module === 'payroll'">
          <Parttime :payroll="module === 'payroll'" />
        </div>
        <Fulltime v-if="module === 'fulltime'" />
        <Performance v-if="module === 'performance'" />
      </main>
    </div>
  </div>
</template>
