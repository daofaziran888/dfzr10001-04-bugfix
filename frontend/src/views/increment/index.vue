<template>
  <section class="page" data-module="increment">
    <header class="page-head">
      <div>
        <h2>增量采集游标管理</h2>
        <p class="page-desc">维护增量采集的水位标记、进度追踪与完成状态，保证采集不重不漏。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记增量游标</button>
        <button class="btn" type="button" @click="exportRows">导出增量采集游标清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section v-if="alignTarget" class="align-panel">
      <h3>标记对齐：{{ alignTarget.游标编号 }}（当前状态 {{ alignTarget.status }}）</h3>
      <p class="align-tip">
        核对来源标识、增量字段、当前水位、上次水位、增量条数与采集时间后再提交；
        提交成功才会替换旧水位，同一采集时间的重复提交只落一次结果。
      </p>
      <form class="filter-bar" @submit.prevent="submitAlign">
        <label class="filter-item">
          <span>来源标识</span>
          <input v-model="alignForm.来源标识" required placeholder="如 SOURCE-A" />
        </label>
        <label class="filter-item">
          <span>增量字段</span>
          <input v-model="alignForm.增量字段" readonly title="以登记为准，不一致会被判为中断" />
        </label>
        <label class="filter-item">
          <span>当前水位</span>
          <input v-model="alignForm.当前水位" required placeholder="如 12:00" />
        </label>
        <label class="filter-item">
          <span>上次水位</span>
          <input v-model="alignForm.上次水位" placeholder="留空则按服务端提交顺序平移" />
        </label>
        <label class="filter-item">
          <span>增量条数</span>
          <input v-model="alignForm.增量条数" required placeholder="如 60" />
        </label>
        <label class="filter-item">
          <span>采集时间</span>
          <input v-model="alignForm.采集时间" required placeholder="如 2026-11-12T23:59:59+08:00" />
        </label>
        <button class="btn primary" type="submit" :disabled="alignSubmitting">提交对齐</button>
        <button class="btn ghost" type="button" @click="alignTarget = null">取消</button>
      </form>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无增量采集游标数据，可先登记增量游标</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条增量采集游标记录</span>
      <span v-if="noticeMessage" class="notice-text">{{ noticeMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'
import { useRefreshStore } from '@/stores/refresh'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/increment'
const columns = ["游标编号", "来源标识", "增量字段", "当前水位", "上次水位", "增量条数", "采集时间", "是否完成"]
const actions = ["标记对齐", "重置游标", "停用游标"]
const statuses = ["草稿", "采集中", "已对齐", "已停用"]
const stats = [{"label": "增量游标总数", "value": 0}, {"label": "采集中游标", "value": 0}, {"label": "已对齐游标", "value": 0}]

const refreshStore = useRefreshStore()

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const noticeMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 标记对齐面板：对齐必须核对并提交水位字段，不能只改状态而留下旧采集时间
const alignTarget = ref<Row | null>(null)
const alignSubmitting = ref(false)
const alignForm = reactive({
  来源标识: '',
  增量字段: '',
  当前水位: '',
  上次水位: '',
  增量条数: '',
  采集时间: '',
})

function nowLocalIso(): string {
  const now = new Date()
  const pad = (value: number) => String(value).padStart(2, '0')
  const offset = -now.getTimezoneOffset()
  const sign = offset >= 0 ? '+' : '-'
  const hours = pad(Math.floor(Math.abs(offset) / 60))
  const minutes = pad(Math.abs(offset) % 60)
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}${sign}${hours}:${minutes}`
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '增量游标登记入口尚未接入审批流'
}

function runAction(action: string, row: Row) {
  if (action === '标记对齐') {
    alignTarget.value = row
    alignForm.来源标识 = String(row.来源标识 ?? '')
    alignForm.增量字段 = String(row.增量字段 ?? '')
    alignForm.当前水位 = ''
    alignForm.上次水位 = String(row.当前水位 ?? '')
    alignForm.增量条数 = ''
    alignForm.采集时间 = nowLocalIso()
    return
  }
  void submitAction(action, row, {})
}

async function submitAlign() {
  if (!alignTarget.value || alignSubmitting.value) {
    return
  }
  alignSubmitting.value = true
  try {
    const ok = await submitAction('标记对齐', alignTarget.value, { ...alignForm })
    if (ok) {
      alignTarget.value = null
    }
  } finally {
    alignSubmitting.value = false
  }
}

async function submitAction(action: string, row: Row, extra: Record<string, string>): Promise<boolean> {
  errorMessage.value = ''
  noticeMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, ...extra } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      const detail = typeof payload?.detail === 'string' ? payload.detail : null
      throw new Error(payload?.message ?? detail ?? '增量采集游标动作未生效，请稍后重试')
    }
    noticeMessage.value = payload.message ?? `增量游标已${action}`
    refreshStore.bump()
    await reload()
    return true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量采集游标操作失败'
    return false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('增量游标列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '增量采集游标列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.align-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.align-panel h3 {
  margin: 0 0 4px;
  font-size: 14px;
}
.align-tip {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--muted);
}
.notice-text {
  color: #067647;
}
</style>
