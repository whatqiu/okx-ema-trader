<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import { api } from '../api'

defineProps({ symbol: String })
const emit = defineEmits(['select'])
const data = ref(null)
const mode = ref('hot')
const period = ref('rolling')
const busy = ref(false)
const error = ref('')
let timer
let disposed = false
const board = computed(() => period.value === 'rolling' ? data.value?.rolling
  : period.value === 'today' ? data.value?.today
    : data.value?.history.days.find(d => d.date === period.value))
const historical = computed(() => !['rolling', 'today'].includes(period.value))
const rows = computed(() => mode.value === 'hot' ? (data.value?.hot || [])
  : (board.value?.[mode.value] || []))
const change = row => mode.value === 'hot' || period.value === 'rolling' ? row.change24h
  : period.value === 'today' ? row.change_today : row.change
const pct = value => value == null ? '—' : `${value > 0 ? '+' : ''}${(value * 100).toFixed(2)}%`
const price = value => value == null ? '—' : Number(value).toLocaleString('en-US', { maximumSignificantDigits: 7 })
const volume = value => value >= 1e8 ? `${(value / 1e8).toFixed(2)} 亿`
  : value >= 1e4 ? `${(value / 1e4).toFixed(1)} 万` : value.toFixed(0)
async function load() {
  if (busy.value || disposed) return
  clearTimeout(timer)
  busy.value = true
  try {
    const result = await api.rankings()
    if (disposed) return
    data.value = result
    if (historical.value && !result.history.days.some(d => d.date === period.value)) period.value = 'today'
    error.value = ''
  } catch (err) {
    error.value = err.message
  } finally {
    busy.value = false
    if (!disposed) timer = setTimeout(load, data.value?.history.loading ? 5000 : 60000)
  }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; clearTimeout(timer) })
</script>

<template>
  <section class="rankings panel panel--pad" aria-label="OKX 热门币种与每日涨跌榜">
    <h2 class="panel__title">热门 / 涨跌榜 <button class="btn btn--ghost btn--mini" :disabled="busy" @click="load">{{ busy ? '读取中…' : '刷新' }}</button></h2>
    <p class="rankings__note">OKX · USDT 永续 · 点击币种查看行情</p>
    <div class="rankings__tabs" aria-label="排行类型">
      <button v-for="tab in [['hot', '热门'], ['gainers', '涨幅榜'], ['losers', '跌幅榜']]"
              :key="tab[0]" :aria-pressed="mode === tab[0]" :class="{ active: mode === tab[0] }"
              @click="mode = tab[0]">{{ tab[1] }}</button>
    </div>
    <label v-if="mode !== 'hot'" class="rankings__period">统计时间
      <select v-model="period" class="input" aria-label="排行统计日期">
        <option value="rolling">最近 24 小时</option>
        <option value="today">今天 · {{ data?.today_date || '北京时间' }}</option>
        <option v-for="day in data?.history.days || []" :key="day.date" :value="day.date">{{ day.date }}</option>
      </select>
    </label>
    <p v-if="error" class="rankings__error" role="alert">{{ error }}{{ data ? '；下方为上次结果，可能已过期。' : '' }}</p>
    <p v-if="mode === 'hot'" class="rankings__note">按近 24h 估算成交额排序，每 5 分钟更新；非 OKX App 人气榜。</p>
    <p v-else class="rankings__note">{{ historical ? '已收盘日线：收盘价相对开盘价' : period === 'today' ? '北京时间 00:00 至今，尚未收盘' : '滚动 24 小时，不等于自然日' }}<br>已覆盖 {{ board?.covered || 0 }} / {{ data?.total || 0 }} 个当前在市合约</p>
    <p v-if="mode !== 'hot' && historical && data?.history.loading" class="rankings__note" role="status">正在补齐日线 {{ data.history.processed }} / {{ data.history.total }}，当前为部分排行。</p>
    <p v-if="mode !== 'hot' && historical && (data?.history.failed || data?.history.error)" class="rankings__error">{{ data.history.error || `${data.history.failed} 个合约日线获取失败，当前排行不完整。` }}</p>
    <p v-if="mode !== 'hot' && historical && data?.history.stale" class="rankings__error">跨日更新中，历史数据尚未刷新。</p>
    <div class="rankings__head"><span>币种 / {{ mode === 'hot' ? '估算成交额 USDT' : historical ? '当日收盘价' : '最新价' }}</span><span>{{ mode === 'hot' ? '24h 涨跌' : '涨跌幅' }}</span></div>
    <div class="rankings__list">
      <button v-for="(row, index) in rows" :key="row.inst_id" class="rankings__row"
              :class="{ selected: symbol === row.base || symbol === row.inst_id }"
              :aria-label="`查看 ${row.base} 行情`" @click="emit('select', row.base)">
        <span class="rankings__rank num">{{ index + 1 }}</span>
        <span class="rankings__coin"><b>{{ row.base }}</b><small class="num">{{ mode === 'hot' ? volume(row.vol_ccy24h) : price(row.last) }}</small></span>
        <strong class="num" :class="change(row) > 0 ? 'tone-up' : change(row) < 0 ? 'tone-down' : ''">{{ pct(change(row)) }}</strong>
      </button>
      <p v-if="!rows.length" class="rankings__empty">{{ busy || data?.history.loading && historical && mode !== 'hot' ? '正在获取 OKX 行情…' : error ? '暂时无法获取排行，请重试' : '该时段暂无符合条件的币种' }}</p>
    </div>
    <p v-if="mode !== 'hot' && historical" class="rankings__note">UTC+8 自然日；不含已下架合约。新上市合约可能没有完整历史。</p>
  </section>
</template>

<style scoped>
.rankings { flex-shrink: 0; }
.rankings__note { color: var(--tx-tertiary); font-size: var(--fs-xs); line-height: 1.6; margin: var(--sp-2) 0; }
.rankings__tabs { display: flex; gap: var(--sp-1); background: var(--bg-sunken); padding: var(--sp-1); border-radius: var(--r-sm); }
.rankings__tabs button { flex: 1; border: 0; border-radius: var(--r-sm); padding: var(--sp-2); color: var(--tx-secondary); background: transparent; cursor: pointer; font: inherit; }
.rankings__tabs .active { background: var(--c-accent-soft); color: var(--c-accent); }
.rankings__period { display: flex; align-items: center; gap: var(--sp-2); font-size: var(--fs-xs); margin-top: var(--sp-3); color: var(--tx-secondary); }
.rankings__period select { flex: 1; min-width: 0; }
.rankings__head { display: flex; justify-content: space-between; gap: var(--sp-2); font-size: var(--fs-2xs); color: var(--tx-tertiary); padding: var(--sp-2) 0; }
.rankings__list { max-height: 330px; overflow-y: auto; }
.rankings__row { width: 100%; display: flex; align-items: center; gap: var(--sp-2); border: 0; border-bottom: 1px solid var(--line-soft); padding: var(--sp-2); background: transparent; color: var(--tx-primary); cursor: pointer; font: inherit; text-align: left; }
.rankings__row:hover { background: var(--bg-hover); }
.rankings__row.selected { background: var(--c-accent-soft); }
.rankings button:focus-visible { outline: 2px solid var(--c-accent); outline-offset: -2px; }
.rankings__rank { width: 20px; color: var(--tx-tertiary); font-size: var(--fs-xs); }
.rankings__coin { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.rankings__coin b { overflow-wrap: anywhere; }
.rankings__coin small { color: var(--tx-tertiary); font-size: var(--fs-2xs); }
.rankings__row strong { font-size: var(--fs-sm); white-space: nowrap; }
.rankings__error { font-size: var(--fs-xs); color: var(--c-warn); line-height: 1.5; margin: var(--sp-2) 0; }
.rankings__empty { padding: var(--sp-4) 0; color: var(--tx-tertiary); font-size: var(--fs-xs); text-align: center; }
</style>
