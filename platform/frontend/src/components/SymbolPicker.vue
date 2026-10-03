<script setup>
// Searchable symbol combobox: debounced server search, keyboard navigation,
// recent picks, and an offline-degraded list (source === 'local').
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '../api'

const props = defineProps({ modelValue: { type: String, default: '' } })
const emit = defineEmits(['update:modelValue', 'select'])

const RECENT_KEY = 'recentSymbols'

const text = ref(props.modelValue)
const open = ref(false)
const items = ref([])
const loading = ref(false)
const source = ref('okx')
const highlight = ref(-1)
const box = ref(null)
let debounce = null

watch(() => props.modelValue, v => { text.value = v })

function recents() {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) || '[]') } catch { return [] }
}
function pushRecent(sym) {
  const list = [sym, ...recents().filter(s => s !== sym)].slice(0, 5)
  localStorage.setItem(RECENT_KEY, JSON.stringify(list))
}

async function search(q) {
  loading.value = true
  try {
    const data = await api.instruments(q)
    items.value = data.rows
    source.value = data.source
    highlight.value = data.rows.length ? 0 : -1
  } catch {
    items.value = []
  }
  loading.value = false
}

function onInput() {
  open.value = true
  clearTimeout(debounce)
  debounce = setTimeout(() => search(text.value.trim()), 200)
}
function onFocus() {
  open.value = true
  search(text.value.trim())
}
function pick(item) {
  const sym = item.inst_id
  pushRecent(sym)
  text.value = sym
  emit('update:modelValue', sym)
  emit('select', sym)
  open.value = false
}
function onKey(e) {
  if (e.key === 'ArrowDown') {
    if (!open.value) { open.value = true; search(text.value.trim()) }
    else highlight.value = Math.min(highlight.value + 1, items.value.length - 1)
    e.preventDefault()
  } else if (e.key === 'ArrowUp') {
    highlight.value = Math.max(highlight.value - 1, -1)
    e.preventDefault()
  } else if (e.key === 'Enter') {
    if (highlight.value >= 0 && items.value[highlight.value]) {
      pick(items.value[highlight.value])
    } else if (text.value.trim()) {
      // No match needed: the backend normalises MU / muusdt / MU-USDT-SWAP.
      pick({ inst_id: text.value.trim() })
    }
  } else if (e.key === 'Escape') {
    open.value = false
  }
}
function onDocClick(e) {
  if (box.value && !box.value.contains(e.target)) open.value = false
}
onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  clearTimeout(debounce)
})

function fmtPct(v) { return v == null ? '' : (v >= 0 ? '+' : '') + (v * 100).toFixed(2) + '%' }
function fmtVol(v) {
  if (!v) return ''
  if (v >= 1e9) return (v / 1e9).toFixed(1) + 'B'
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M'
  if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K'
  return v.toFixed(0)
}
function fmtPrice(v) {
  if (v == null) return ''
  if (v >= 100) return v.toFixed(1)
  if (v >= 1) return v.toFixed(3)
  return Number(v).toPrecision(3)
}
</script>

<template>
  <div ref="box" class="picker">
    <input v-model="text" class="symbol-input" placeholder="搜索币种，如 BTC / MU"
           @input="onInput" @focus="onFocus" @keydown="onKey" />
    <div v-if="open" class="dropdown">
      <div v-if="source === 'local'" class="dd-note warn">
        离线模式：只显示本地已有的币种
      </div>
      <template v-if="!text.trim() && recents().length">
        <div class="dd-label">最近使用</div>
        <div v-for="s in recents()" :key="'r-' + s" class="dd-item"
             @mousedown.prevent="pick({ inst_id: s })">
          <b>{{ s.replace('-USDT-SWAP', '') }}</b>
          <span class="dim">/ USDT 永续</span>
        </div>
        <div class="dd-label">热门（按 24h 成交额）</div>
      </template>
      <div v-for="(it, i) in items" :key="it.inst_id" class="dd-item"
           :class="{ hl: i === highlight }"
           @mousedown.prevent="pick(it)" @mouseenter="highlight = i">
        <b>{{ it.base }}</b>
        <span class="dim">/ USDT 永续</span>
        <span class="dd-right">
          <span v-if="it.change24h != null" :class="it.change24h >= 0 ? 'up' : 'down'">
            {{ fmtPct(it.change24h) }}
          </span>
          <span v-if="it.last != null">{{ fmtPrice(it.last) }}</span>
          <span class="dim">{{ fmtVol(it.vol_ccy24h) }}</span>
        </span>
      </div>
      <div v-if="loading" class="dd-note">搜索中…</div>
      <div v-else-if="!items.length && text.trim()" class="dd-note">
        无匹配 — 回车直接加载「{{ text.trim() }}」
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker { position: relative; }
.dropdown {
  position: absolute; top: calc(100% + 4px); left: 0; z-index: 50;
  width: 320px; max-height: 380px; overflow-y: auto;
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}
.dd-label {
  padding: 5px 10px 2px; font-size: 10px; color: var(--dim);
  text-transform: uppercase; letter-spacing: 0.5px;
}
.dd-item {
  display: flex; align-items: baseline; gap: 6px;
  padding: 6px 10px; cursor: pointer; white-space: nowrap;
}
.dd-item.hl { background: rgba(240, 185, 11, 0.12); }
.dd-right { margin-left: auto; display: flex; gap: 10px; font-size: 12px; }
.dd-note { padding: 8px 10px; color: var(--dim); font-size: 12px; }
</style>
