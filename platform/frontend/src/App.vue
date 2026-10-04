<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from './api'
import KlineChart from './components/KlineChart.vue'
import SymbolPicker from './components/SymbolPicker.vue'

const BARS = ['1m', '3m', '5m', '15m', '30m', '1H', '4H', '1D']
const BAR_MS = {
  '1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000,
  '30m': 1800000, '1H': 3600000, '4H': 14400000, '1D': 86400000,
}

const symbol = ref(localStorage.getItem('symbol') || 'MU')
const bar = ref(localStorage.getItem('bar') || '5m')
const rows = ref([])
const ticker = ref(null)
const signals = ref([])
const orders = ref([])
const backtests = ref([])
const stats = ref(null)
const equityRows = ref([])
const account = ref(null)
const error = ref('')
const notice = ref('')
const loading = ref(false)
const syncing = ref(false)
const lastUpdate = ref(null)
const autoRefresh = ref(localStorage.getItem('autoRefresh') !== '0')
const showSettings = ref(false)
const proxyInput = ref('')
const resolvedProxy = ref('')

// --- trade ticket ----------------------------------------------------------
const rightTab = ref('trade')
const tradeSide = ref('long')
const tradeNotional = ref(100)
const tradeLeverage = ref(5)
const trading = ref(false)
const closingId = ref(null)

// --- backtest ---------------------------------------------------------------
const btDays = ref(30)
const btFee = ref(5)
const btSlip = ref(3)
const btRunning = ref(false)
const btResult = ref(null)

// --- auto-trade ---------------------------------------------------------------
const auto = ref(null)
const autoNotional = ref(100)
const autoLeverage = ref(5)
const autoBusy = ref(false)

// --- order history (own ref + filters, independent from chart markers) ------
const historyOrders = ref([])
const ordStatus = ref('')
const ordOnlyCurrent = ref(false)

// --- liveness + circuit breaker -------------------------------------------
// The backend owns the truth about whether OKX is reachable; this only renders
// it. `wasOffline` exists so the banner can announce the recovery instead of
// silently vanishing, which is what makes an outage feel like a glitch.
const status = ref(null)
const wasOffline = ref(false)
const riskToast = ref('')

const connOk = computed(() => !!status.value?.connected)
const offlineFor = computed(() => status.value?.market?.offline_for_s ?? 0)
const losingStreak = computed(() => status.value?.risk?.losing_streak ?? 0)
const riskActive = computed(() => !!status.value?.risk?.limits?.enabled)

function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0))
  if (s < 60) return `${s}秒`
  if (s < 3600) return `${Math.floor(s / 60)}分${s % 60}秒`
  return `${Math.floor(s / 3600)}小时${Math.floor((s % 3600) / 60)}分`
}

function lastOkText() {
  const at = status.value?.market?.last_ok_at
  if (!at) return '尚未成功连接'
  return new Date(at).toLocaleTimeString('zh-CN', { hour12: false })
}

async function loadStatus() {
  try {
    const s = await api.status()
    const was = status.value?.connected
    status.value = s
    if (was === true && !s.connected) wasOffline.value = true
    if (s.connected && wasOffline.value) {
      wasOffline.value = false
      notice.value = '网络已恢复'
      setTimeout(() => (notice.value = ''), 3000)
    }
    // A risk close that happened in the background is the single most important
    // thing this panel can tell you — a position died while you were away.
    const closed = s?.risk?.last?.closed || []
    if (closed.length) {
      const last = closed[closed.length - 1]
      riskToast.value = `风控平仓：${last.reason}`
    }
  } catch {
    // Backend itself unreachable. Leave the previous state alone so the badge
    // does not flip to a false "offline from OKX" — the process is the problem.
    status.value = null
  }
}

const orderSummary = computed(() => {
  const closed = historyOrders.value.filter(o => o.pnl != null)
  const total = closed.reduce((s, o) => s + o.pnl, 0)
  const wins = closed.filter(o => o.pnl > 0).length
  return {
    count: historyOrders.value.length,
    closed: closed.length,
    winRate: closed.length ? wins / closed.length : null,
    total,
  }
})

let timers = []

const changePct = computed(() => {
  const t = ticker.value
  if (!t || !t.open24h) return null
  return (t.last - t.open24h) / t.open24h * 100
})

const markers = computed(() => {
  const sig = signals.value
    .filter(s => s.side && s.price)
    .map(s => ({ ts: s.ts, side: s.side, price: s.price }))
  const trades = orders.value
    .filter(o => o.entry_price)
    .map(o => ({ ts: o.opened_at, side: o.side, price: o.entry_price }))
  return [...sig, ...trades]
})

const equityPath = computed(() => {
  const pts = equityRows.value
  if (pts.length < 2) return ''
  const W = 100, H = 28
  const vals = pts.map(p => p.equity)
  const lo = Math.min(...vals), hi = Math.max(...vals)
  const span = hi - lo || 1
  return pts.map((p, i) =>
    `${(i / (pts.length - 1) * W).toFixed(1)},${(H - 2 - (p.equity - lo) / span * (H - 4)).toFixed(1)}`
  ).join(' ')
})

const equityTrendUp = computed(() => {
  const pts = equityRows.value
  return pts.length >= 2 && pts[pts.length - 1].equity >= pts[0].equity
})

function fmtNum(v, digits = 4) {
  if (v == null || Number.isNaN(v)) return '-'
  if (Math.abs(v) >= 1000) return v.toFixed(1)
  if (Math.abs(v) >= 1) return v.toFixed(digits > 3 ? 3 : digits)
  return Number(v).toPrecision(4)
}
function fmtUsdt(v) {
  return v == null ? '-' : (v < 0 ? '-' : '') + Math.abs(v).toFixed(2)
}
function fmtPct(v) { return v == null ? '-' : (v * 100).toFixed(2) + '%' }
function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
         `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

async function guard(fn, silent = false) {
  try {
    if (!silent) error.value = ''
    await fn()
  } catch (e) {
    if (!silent) error.value = e.message
  }
}
function flash(msg) {
  notice.value = msg
  setTimeout(() => { if (notice.value === msg) notice.value = '' }, 4000)
}

// sync=true asks the backend to fill gaps from OKX first — needed on user
// actions. Background polls use sync=false: the server-side poller already
// keeps data fresh, and two pollers (browser + server) would double our
// 20req/2s rate budget for nothing.
async function loadCandles(sync) {
  await guard(async () => {
    syncing.value = true
    const data = await api.candles(symbol.value, bar.value, 500, sync)
    rows.value = data.rows
    lastUpdate.value = Date.now()
  })
  syncing.value = false
}
async function loadTicker() {
  await guard(async () => {
    ticker.value = await api.ticker(symbol.value)
    mergeTickIntoCandle()
  }, true)
}

// The server now fetches candles only at bar closes (to save rate budget);
// the forming bar's live shape is reconstructed HERE from the ticker: every
// tick updates close/high/low, and crossing a bar boundary opens a new bar.
// Visually identical to server-side refresh, at zero extra OKX calls.
function mergeTickIntoCandle() {
  const t = ticker.value
  if (!t || !t.last || !rows.value.length) return
  const step = BAR_MS[bar.value]
  if (!step) return
  const bucket = Math.floor(Date.now() / step) * step
  const last = rows.value[rows.value.length - 1]
  if (last[0] === bucket) {
    last[4] = t.last
    if (t.last > last[2]) last[2] = t.last
    if (t.last < last[3]) last[3] = t.last
  } else if (bucket > last[0]) {
    rows.value.push([bucket, t.last, t.last, t.last, t.last, 0, 0])
    if (rows.value.length > 500) rows.value.shift()
  }
  rows.value = rows.value.slice()  // nudge reactivity (nested-array mutation)
}
async function loadPanels() {
  await guard(async () => {
    const [s, o, b, st, eq, acc] = await Promise.all([
      api.signals(symbol.value, 50), api.orders({ symbol: symbol.value, limit: 50 }),
      api.backtests(symbol.value, 10), api.stats(),
      api.equity(300), api.account(),
    ])
    signals.value = s.rows
    orders.value = o.rows
    backtests.value = b.rows
    stats.value = st
    equityRows.value = eq.rows
    account.value = acc
    auto.value = await api.autotrade()
  }, true)
}

async function loadOrders() {
  await guard(async () => {
    const data = await api.orders({
      symbol: ordOnlyCurrent.value ? symbol.value : undefined,
      status: ordStatus.value || undefined,
      limit: 300,
    })
    historyOrders.value = data.rows
  }, true)
}

watch(rightTab, tab => { if (tab === 'history') loadOrders() })

async function applySymbol() {
  localStorage.setItem('symbol', symbol.value)
  localStorage.setItem('bar', bar.value)
  loading.value = true
  await loadCandles(true)
  await Promise.all([loadTicker(), loadPanels()])
  if (rightTab.value === 'history') await loadOrders()
  loading.value = false
}

// --- trading ---------------------------------------------------------------
// The price a market order would actually fill at RIGHT NOW: long crosses to
// the ask, short hits the bid. Shown in the ticket so the user is never
// surprised that the fill differs from the mid price on the chart.
const refPrice = computed(() => {
  const t = ticker.value
  if (!t) return null
  return tradeSide.value === 'long' ? t.ask : t.bid
})

// Percentage buttons: pct of AVAILABLE balance posted as margin, levered up
// into notional — the same semantics exchange UIs use for their % shortcuts.
function setNotionalPct(pct) {
  const avail = account.value?.available
  if (!avail) return
  tradeNotional.value = Math.max(1, Math.floor(avail * pct * tradeLeverage.value))
}

async function openTrade() {
  trading.value = true
  await guard(async () => {
    const res = await api.openTrade({
      symbol: symbol.value, side: tradeSide.value,
      notional: Number(tradeNotional.value), leverage: Number(tradeLeverage.value),
    })
    flash(`已开${tradeSide.value === 'long' ? '多' : '空'} @ ${fmtNum(res.entry_price)} · 手续费 ${res.fee.toFixed(2)} USDT`)
    await loadPanels()
  })
  trading.value = false
}

async function closeTrade(pos) {
  closingId.value = pos.id
  await guard(async () => {
    const res = await api.closeTrade(pos.id)
    flash(`已平仓 #${pos.id} @ ${fmtNum(res.exit_price)} · 盈亏 ${res.pnl >= 0 ? '+' : ''}${res.pnl.toFixed(2)} USDT`)
    await loadPanels()
  })
  closingId.value = null
}

async function doResetAccount() {
  if (!window.confirm('确定重置模拟账户？所有持仓和历史订单将被清空。')) return
  await guard(async () => {
    await api.resetAccount(10000)
    flash('账户已重置为 10,000 USDT')
    await loadPanels()
  })
}

// --- backtest ----------------------------------------------------------------
async function runBacktest() {
  btRunning.value = true
  btResult.value = null
  await guard(async () => {
    btResult.value = await api.runBacktest({
      symbol: symbol.value, days: btDays.value,
      fee_bps: Number(btFee.value), slippage_bps: Number(btSlip.value),
    })
    await loadPanels()
  })
  btRunning.value = false
}

// --- auto-trade ----------------------------------------------------------------
async function loadAuto() {
  await guard(async () => {
    auto.value = await api.autotrade()
    autoNotional.value = auto.value.notional
    autoLeverage.value = auto.value.leverage
  }, true)
}
async function toggleAutoTrade() {
  if (!auto.value) return
  autoBusy.value = true
  await guard(async () => {
    auto.value = await api.setAutotrade({
      enabled: !auto.value.enabled,
      symbol: symbol.value,
      notional: Number(autoNotional.value),
      leverage: Number(autoLeverage.value),
    })
    flash(auto.value.enabled
      ? `自动交易已开启：${auto.value.symbol} · 每根5m收盘评估信号`
      : '自动交易已停止（已有持仓不会自动平仓）')
    await loadPanels()
  })
  autoBusy.value = false
}
async function saveAutoParams() {
  autoBusy.value = true
  await guard(async () => {
    auto.value = await api.setAutotrade({
      notional: Number(autoNotional.value),
      leverage: Number(autoLeverage.value),
    })
    flash('自动交易参数已保存')
  })
  autoBusy.value = false
}

// --- settings ------------------------------------------------------------------
async function loadSettings() {
  await guard(async () => {
    const s = await api.settings()
    proxyInput.value = s.proxy ?? ''
    resolvedProxy.value = s.resolved_proxy || '(直连)'
  }, true)
}
async function saveProxy() {
  await guard(async () => {
    await api.saveSettings({ proxy: proxyInput.value })
    await loadSettings()
    flash('代理设置已保存')
  })
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  localStorage.setItem('autoRefresh', autoRefresh.value ? '1' : '0')
  setupTimers()
}
function setupTimers() {
  timers.forEach(clearInterval)
  timers = autoRefresh.value ? [
    setInterval(() => loadCandles(false), 5000),
    setInterval(loadTicker, 3000),
    setInterval(loadPanels, 10000),
    // Liveness is deliberately NOT tied to the auto-refresh switch. Turning
    // refresh off means "stop asking the market for candles", but the one thing
    // you still need to know is whether the connection died while you were away.
    setInterval(loadStatus, 4000),
  ] : [
    setInterval(loadStatus, 4000),
  ]
}

onMounted(async () => {
  await applySymbol()
  await Promise.all([loadSettings(), loadAuto()])
  await loadStatus()
  setupTimers()
})
onBeforeUnmount(() => timers.forEach(clearInterval))
</script>

<template>
  <div class="terminal">
    <!-- ======================= header ======================= -->
    <header class="topbar">
      <div class="brand">OKX EMA Trader <span class="badge">模拟盘</span></div>
      <div class="symbol-box">
        <SymbolPicker v-model="symbol" @select="applySymbol" />
        <select v-model="bar" class="bar-select" @change="applySymbol">
          <option v-for="b in BARS" :key="b" :value="b">{{ b }}</option>
        </select>
        <button class="btn" :disabled="loading" @click="applySymbol">
          {{ loading ? '同步中…' : '加载' }}
        </button>
      </div>
      <div class="ticker-strip" v-if="ticker">
        <span class="last" :class="(changePct ?? 0) >= 0 ? 'up' : 'down'">
          {{ fmtNum(ticker.last) }}
        </span>
        <span :class="(changePct ?? 0) >= 0 ? 'up' : 'down'">
          {{ changePct == null ? '-' : (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%' }}
        </span>
        <span class="dim">24h高 {{ fmtNum(ticker.high24h) }}</span>
        <span class="dim">24h低 {{ fmtNum(ticker.low24h) }}</span>
        <span class="dim">量 {{ fmtNum(ticker.vol24h, 0) }}</span>
      </div>
      <div class="topbar-right">
        <span class="conn-badge" v-if="status"
              :class="connOk ? 'ok' : 'bad'"
              :title="connOk
                ? `已连接 OKX，最后成功 ${lastOkText()}`
                : `断网 ${fmtDuration(offlineFor)}｜最后成功 ${lastOkText()}｜${status.market?.last_error || status.auto?.last_error || '未知原因'}`">
          <span class="dot"></span>
          {{ connOk ? '已连接' : '断网' + fmtDuration(offlineFor) }}
        </span>
        <span class="conn-badge" v-if="status && riskActive"
              :class="losingStreak > 0 ? 'warn' : 'muted'"
              :title="`风控：单仓浮亏 >${status.risk.limits.max_loss_pct}% 强平｜账户回撤 >${status.risk.limits.max_daily_loss_pct}% 强平｜连亏 ${status.risk.limits.max_consecutive_losses} 次停手`">
          风控 {{ losingStreak > 0 ? `连亏${losingStreak}` : '启用' }}
        </span>
        <button class="btn ghost" :class="{ on: autoRefresh }" @click="toggleAuto"
                :title="autoRefresh ? '自动刷新：开' : '自动刷新：关'">
          {{ autoRefresh ? '⟳ 自动' : '⏸ 手动' }}
        </button>
        <button v-if="!autoRefresh" class="btn ghost" @click="applySymbol">刷新</button>
        <button class="btn ghost" @click="showSettings = !showSettings">⚙</button>
      </div>
    </header>

    <!-- An outage must never look like a quiet market: say it out loud, with a
         duration and a cause, for as long as it lasts. -->
    <div v-if="status && !connOk" class="offline-banner">
      <strong>已与 OKX 断线 {{ fmtDuration(offlineFor) }}</strong>
      <span>最后成功连接：{{ lastOkText() }}</span>
      <span class="dim">{{ status.market?.last_error || status.auto?.last_error || '原因未知' }}</span>
      <span class="dim">断线期间无法获取实时价格，浮亏按开仓价估算；恢复后风控会立即重新判定。</span>
    </div>
    <div v-if="riskToast" class="risk-banner">{{ riskToast }}</div>

    <div v-if="showSettings" class="settings-bar">
      <span class="dim">代理（当前生效: {{ resolvedProxy }}）</span>
      <input v-model="proxyInput" class="symbol-input" placeholder="留空=系统代理，如 http://127.0.0.1:6088" />
      <button class="btn" @click="saveProxy">保存</button>
      <button class="btn ghost" @click="showSettings = false">收起</button>
    </div>

    <div v-if="error" class="error-banner">{{ error }}</div>
    <div v-if="notice" class="notice-banner">{{ notice }}</div>

    <!-- ======================= body ======================= -->
    <main class="grid">
      <!-- left: account + 24h + signals -->
      <aside class="col left">
        <section class="panel account-panel" v-if="account">
          <h3>模拟账户 <button class="mini" @click="doResetAccount" title="清空并重置为1万">重置</button></h3>
          <div class="equity-line">
            <b class="equity-num">{{ fmtUsdt(account.equity) }}</b>
            <svg v-if="equityPath" viewBox="0 0 100 28" preserveAspectRatio="none" class="spark">
              <polyline :points="equityPath" fill="none"
                        :stroke="equityTrendUp ? '#0ecb81' : '#f6465d'" stroke-width="1.5" />
            </svg>
          </div>
          <div class="kv">
            <div><span>可用</span><b>{{ fmtUsdt(account.available) }}</b></div>
            <div><span>已用保证金</span><b>{{ fmtUsdt(account.margin_used) }}</b></div>
            <div><span>未实现盈亏</span>
              <b :class="account.unrealised_pnl >= 0 ? 'up' : 'down'">
                {{ account.unrealised_pnl >= 0 ? '+' : '' }}{{ fmtUsdt(account.unrealised_pnl) }}
              </b></div>
            <div><span>已实现盈亏</span>
              <b :class="account.realised_pnl >= 0 ? 'up' : 'down'">
                {{ account.realised_pnl >= 0 ? '+' : '' }}{{ fmtUsdt(account.realised_pnl) }}
              </b></div>
          </div>
        </section>
        <section class="panel">
          <h3>24小时</h3>
          <div class="kv" v-if="ticker">
            <div><span>买一</span><b class="up">{{ fmtNum(ticker.bid) }}</b></div>
            <div><span>卖一</span><b class="down">{{ fmtNum(ticker.ask) }}</b></div>
            <div><span>24h开</span><b>{{ fmtNum(ticker.open24h) }}</b></div>
            <div><span>24h额</span><b>{{ fmtNum(ticker.vol_ccy24h, 0) }}</b></div>
          </div>
          <div class="dim" v-else>等待行情…</div>
        </section>
        <section class="panel grow">
          <h3>信号 <span class="dim">({{ signals.length }})</span></h3>
          <div class="scroll">
            <div v-for="s in signals" :key="s.id" class="row">
              <span class="dim">{{ fmtTime(s.ts) }}</span>
              <b :class="s.side === 'short' ? 'down' : 'up'">
                {{ s.side === 'short' ? '空' : (s.side === 'long' ? '多' : '—') }}
              </b>
              <span>{{ fmtNum(s.price) }}</span>
              <span class="dim reason">{{ s.reason }}</span>
            </div>
            <div v-if="!signals.length" class="dim">暂无信号记录</div>
          </div>
        </section>
      </aside>

      <!-- center: chart -->
      <section class="col center">
        <div class="chart-box">
          <KlineChart :rows="rows" :markers="markers" :ema-fast="9" :ema-slow="26" />
        </div>
        <div class="statusline">
          <span :class="syncing ? 'warn' : 'dim'">
            {{ syncing ? '⟳ 正在从 OKX 补拉缺口…' : `本地K线 ${rows.length} 根 · 服务端每5s刷新` }}
          </span>
          <span class="dim" v-if="stats">
            数据库 {{ (stats.size_bytes / 1048576).toFixed(1) }} MB ·
            K线 {{ stats.counts.candles }} · 订单 {{ stats.counts.orders }} · 信号 {{ stats.counts.signals }}
          </span>
        </div>
      </section>

      <!-- right: trade / backtest tabs -->
      <aside class="col right">
        <div class="tabs">
          <button :class="{ active: rightTab === 'trade' }" @click="rightTab = 'trade'">交易</button>
          <button :class="{ active: rightTab === 'backtest' }" @click="rightTab = 'backtest'">回测</button>
          <button :class="{ active: rightTab === 'history' }" @click="rightTab = 'history'">历史</button>
        </div>

        <!-- ============ trade tab ============ -->
        <template v-if="rightTab === 'trade'">
          <section class="panel auto-panel" :class="{ live: auto?.enabled }">
            <div class="auto-head">
              <h3>自动交易 <span class="badge" v-if="auto?.enabled">运行中</span></h3>
              <button class="btn auto-toggle" :class="{ on: auto?.enabled }"
                      :disabled="autoBusy" @click="toggleAutoTrade">
                {{ auto?.enabled ? '停止' : '启动' }}
              </button>
            </div>
            <div class="auto-meta dim">
              策略：15m 趋势 + ADX&gt;20 + 5m EMA20/60 交叉 · 每根 5m 收盘评估 ·
              无信号不操作
            </div>
            <div class="auto-params">
              <label>每笔 USDT
                <input type="number" v-model.number="autoNotional" min="1" step="10"
                       @change="saveAutoParams" />
              </label>
              <label>杠杆
                <input type="number" v-model.number="autoLeverage" min="1" max="20"
                       @change="saveAutoParams" />
              </label>
            </div>
            <div v-if="auto?.last_action" class="auto-meta">最近动作：{{ auto.last_action }}</div>
            <div v-if="auto?.last_error" class="auto-err">最近错误：{{ auto.last_error }}</div>
          </section>
          <section class="panel">
            <div class="side-pick">
              <button class="side-btn long" :class="{ active: tradeSide === 'long' }"
                      @click="tradeSide = 'long'">开多</button>
              <button class="side-btn short" :class="{ active: tradeSide === 'short' }"
                      @click="tradeSide = 'short'">开空</button>
            </div>
            <div class="ref-price" v-if="refPrice">
              <span class="dim">市价成交参考</span>
              <b :class="tradeSide === 'long' ? 'up' : 'down'">{{ fmtNum(refPrice) }}</b>
              <span class="dim">（{{ tradeSide === 'long' ? '卖一' : '买一' }}）</span>
            </div>
            <div class="ticket">
              <label>名义价值 USDT
                <input type="number" v-model.number="tradeNotional" min="1" step="10" />
              </label>
              <div class="chips">
                <button v-for="n in [100, 500, 1000, 5000]" :key="n" class="chip"
                        :class="{ on: tradeNotional === n }"
                        @click="tradeNotional = n">{{ n }}</button>
              </div>
              <div class="chips" v-if="account">
                <button v-for="p in [10, 25, 50, 100]" :key="p" class="chip"
                        title="按可用余额的百分比 × 当前杠杆"
                        @click="setNotionalPct(p / 100)">{{ p }}%</button>
                <span class="dim chips-note">可用 {{ fmtUsdt(account.available) }}</span>
              </div>
              <label>杠杆 <b class="accent-text">{{ tradeLeverage }}x</b>
                <input type="range" v-model.number="tradeLeverage" min="1" max="20" class="slider" />
              </label>
              <div class="ticket-meta dim">
                保证金 ≈ {{ (tradeNotional / tradeLeverage).toFixed(2) }} ·
                手续费 ≈ {{ (tradeNotional * 0.0005).toFixed(2) }}
                <template v-if="ticker"> · 预估强平
                  {{ tradeSide === 'long'
                     ? fmtNum((ticker.ask || 0) * (1 - 1 / tradeLeverage))
                     : fmtNum((ticker.bid || 0) * (1 + 1 / tradeLeverage)) }}
                </template>
              </div>
              <button class="btn submit" :class="tradeSide" :disabled="trading" @click="openTrade">
                {{ trading ? '下单中…' : (tradeSide === 'long' ? '买入开多' : '卖出开空') }}
              </button>
            </div>
          </section>
          <section class="panel grow">
            <h3>持仓 <span class="dim">({{ account?.positions?.length || 0 }})</span></h3>
            <div class="scroll">
              <div v-for="p in account?.positions || []" :key="p.id" class="pos-card">
                <div class="pos-head">
                  <b :class="p.side === 'short' ? 'down' : 'up'">
                    {{ p.side === 'short' ? '空' : '多' }} {{ p.inst_id }}
                  </b>
                  <span class="dim">{{ p.leverage }}x</span>
                  <b :class="p.unrealised_pnl >= 0 ? 'up' : 'down'">
                    {{ p.unrealised_pnl >= 0 ? '+' : '' }}{{ fmtUsdt(p.unrealised_pnl) }}
                  </b>
                </div>
                <div class="pos-meta dim">
                  开仓 {{ fmtNum(p.entry_price) }} · 标记 {{ fmtNum(p.mark_price) }} ·
                  强平 ≈ {{ fmtNum(p.liq_price) }}
                </div>
                <div class="pos-meta dim">
                  名义 {{ fmtUsdt(p.notional) }} · 保证金 {{ fmtUsdt(p.margin) }} · {{ fmtTime(p.opened_at) }}
                </div>
                <button class="btn close-btn" :disabled="closingId === p.id" @click="closeTrade(p)">
                  {{ closingId === p.id ? '平仓中…' : '市价平仓' }}
                </button>
              </div>
              <div v-if="!(account?.positions?.length)" class="dim">暂无持仓</div>
            </div>
          </section>
        </template>

        <!-- ============ backtest tab ============ -->
        <template v-if="rightTab === 'backtest'">
          <section class="panel">
            <div class="bt-form">
              <label>天数 <input type="number" v-model.number="btDays" min="1" max="365" /></label>
              <label>费率 <input type="number" v-model.number="btFee" min="0" step="0.5" /> bps</label>
              <label>滑点 <input type="number" v-model.number="btSlip" min="0" step="0.5" /> bps</label>
              <button class="btn accent" :disabled="btRunning" @click="runBacktest">
                {{ btRunning ? '回测中…' : '运行回测' }}
              </button>
            </div>
            <div v-if="btResult" class="bt-result">
              <div><span>净收益</span>
                <b :class="btResult.net_return > 0 ? 'up' : 'down'">{{ fmtPct(btResult.net_return) }}</b></div>
              <div><span>交易</span><b>{{ btResult.trades }} 笔 · 胜率 {{ fmtPct(btResult.win_rate) }}</b></div>
              <div><span>回撤(账户)</span><b class="down">{{ fmtPct(btResult.max_drawdown_account) }}</b></div>
              <div><span>保本成本</span><b>{{ btResult.breakeven_bps?.toFixed(1) }} bps</b></div>
              <div :class="btResult.net_return > 0 ? 'up' : 'down'" class="verdict">
                {{ btResult.verdict }}
              </div>
            </div>
          </section>
          <section class="panel grow">
            <h3>历史回测</h3>
            <div class="scroll">
              <div v-for="b in backtests" :key="b.id" class="row">
                <span class="dim">{{ fmtTime(b.created_at) }}</span>
                <span>{{ b.days }}d</span>
                <b :class="b.result.net_return > 0 ? 'up' : 'down'">{{ fmtPct(b.result.net_return) }}</b>
              </div>
              <div v-if="!backtests.length" class="dim">暂无回测记录</div>
            </div>
          </section>
        </template>

        <!-- ============ history tab ============ -->
        <template v-if="rightTab === 'history'">
          <section class="panel grow">
            <h3>订单记录 <span class="dim">({{ historyOrders.length }})</span></h3>
            <div class="hist-filters">
              <select v-model="ordStatus" class="bar-select" @change="loadOrders">
                <option value="">全部状态</option>
                <option value="open">持仓中</option>
                <option value="closed">已平仓</option>
              </select>
              <label class="chk dim">
                <input type="checkbox" v-model="ordOnlyCurrent" @change="loadOrders" />
                仅当前币种
              </label>
              <button class="btn ghost mini-refresh" @click="loadOrders">刷新</button>
            </div>
            <div class="hist-summary" v-if="orderSummary.closed">
              <span>已平 {{ orderSummary.closed }} 笔</span>
              <span>胜率 {{ orderSummary.winRate == null ? '-' : (orderSummary.winRate * 100).toFixed(0) + '%' }}</span>
              <span>合计
                <b :class="orderSummary.total >= 0 ? 'up' : 'down'">
                  {{ orderSummary.total >= 0 ? '+' : '' }}{{ fmtUsdt(orderSummary.total) }}
                </b>
              </span>
            </div>
            <div class="scroll">
              <div v-for="o in historyOrders" :key="o.id" class="order-card">
                <div class="or-line1">
                  <b :class="o.side === 'short' ? 'down' : 'up'">
                    {{ o.side === 'short' ? '空' : '多' }}
                  </b>
                  <span>{{ o.inst_id.replace('-USDT-SWAP', '') }}</span>
                  <span class="dim">{{ o.leverage }}x</span>
                  <span class="dim or-time">{{ fmtTime(o.opened_at) }}</span>
                  <b v-if="o.pnl != null" :class="o.pnl >= 0 ? 'up' : 'down'">
                    {{ o.pnl >= 0 ? '+' : '' }}{{ fmtUsdt(o.pnl) }}
                    <span class="dim">({{ o.pnl_pct == null ? '-' : (o.pnl_pct * 100).toFixed(1) + '%' }})</span>
                  </b>
                  <span v-else class="warn">持仓中</span>
                </div>
                <div class="or-line2 dim">
                  {{ fmtNum(o.entry_price) }} → {{ o.exit_price ? fmtNum(o.exit_price) : '…' }}
                  · 名义 {{ fmtUsdt(o.notional) }}
                  <template v-if="o.closed_at"> · 平仓 {{ fmtTime(o.closed_at) }}</template>
                </div>
              </div>
              <div v-if="!historyOrders.length" class="dim">暂无符合条件的订单</div>
            </div>
          </section>
        </template>
      </aside>
    </main>
  </div>
</template>

<style>
:root {
  --bg: #0b0e11;
  --panel: #161a1e;
  --border: #2b3139;
  --text: #eaecef;
  --dim: #848e9c;
  --up: #0ecb81;
  --down: #f6465d;
  --warn: #f0b90b;
  --accent: #f0b90b;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text);
  font: 13px/1.5 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
}
.up { color: var(--up); }
.down { color: var(--down); }
.dim { color: var(--dim); font-weight: normal; }
.warn { color: var(--accent); }
.accent-text { color: var(--accent); }

.terminal { display: flex; flex-direction: column; height: 100vh; }
.topbar {
  display: flex; align-items: center; gap: 18px; padding: 0 14px;
  height: 52px; background: var(--panel); border-bottom: 1px solid var(--border);
}
.brand { font-weight: 700; font-size: 15px; color: var(--accent); white-space: nowrap; }
.badge {
  font-size: 10px; background: var(--accent); color: #0b0e11;
  border-radius: 3px; padding: 1px 5px; vertical-align: 2px;
}
.symbol-box { display: flex; gap: 6px; }
.symbol-input, .bar-select, .bt-form input, .ticket input[type=number], .settings-bar input {
  background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 6px 8px; border-radius: 4px; width: 170px; outline: none;
}
.bar-select { width: 70px; }
.btn {
  background: var(--border); border: none; color: var(--text);
  padding: 6px 14px; border-radius: 4px; cursor: pointer;
}
.btn:hover { background: #3a424c; }
.btn:disabled { opacity: 0.5; cursor: default; }
.btn.accent { background: var(--accent); color: #0b0e11; font-weight: 600; }
.btn.ghost { background: transparent; border: 1px solid var(--border); padding: 4px 10px; }
.btn.ghost.on { border-color: var(--accent); color: var(--accent); }
.btn.mini, button.mini {
  background: transparent; border: 1px solid var(--border); color: var(--dim);
  font-size: 10px; border-radius: 3px; padding: 1px 6px; cursor: pointer; margin-left: 6px;
}
.ticker-strip { display: flex; gap: 14px; align-items: baseline; }
.ticker-strip .last { font-size: 18px; font-weight: 700; }
.topbar-right { margin-left: auto; display: flex; gap: 6px; }

.settings-bar {
  display: flex; gap: 8px; align-items: center; padding: 6px 14px;
  background: var(--panel); border-bottom: 1px solid var(--border);
}
.settings-bar input { flex: 1; max-width: 420px; }

.error-banner {
  background: rgba(246, 70, 93, 0.15); color: var(--down);
  padding: 6px 14px; border-bottom: 1px solid var(--border);
}
.notice-banner {
  background: rgba(14, 203, 129, 0.12); color: var(--up);
  padding: 6px 14px; border-bottom: 1px solid var(--border);
}

/* Liveness badge. Colour alone would not do: a red dot and a green dot are the
   same shape, so the label carries the state and the dot only reinforces it. */
.conn-badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 8px; border-radius: 3px; font-size: 12px;
  border: 1px solid var(--border); color: var(--dim);
  white-space: nowrap; cursor: default;
}
.conn-badge .dot {
  width: 6px; height: 6px; border-radius: 50%; background: currentColor;
  flex: 0 0 auto;
}
.conn-badge.ok { color: var(--up); border-color: rgba(14, 203, 129, 0.4); }
.conn-badge.bad {
  color: var(--down); border-color: var(--down);
  background: rgba(246, 70, 93, 0.12);
}
/* A losing streak is not a failure state — it is a caution. */
.conn-badge.warn { color: var(--warn); border-color: rgba(240, 185, 11, 0.5); }
.conn-badge.muted { color: var(--dim); }

.offline-banner {
  display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  background: rgba(246, 70, 93, 0.15); color: var(--down);
  padding: 7px 14px; border-bottom: 1px solid var(--border);
}
.offline-banner strong { font-weight: 600; }
.offline-banner .dim { color: var(--dim); font-size: 12px; }

.risk-banner {
  background: rgba(240, 185, 11, 0.14); color: var(--warn);
  padding: 6px 14px; border-bottom: 1px solid var(--border);
}

.grid {
  flex: 1; display: grid; gap: 4px; padding: 4px;
  grid-template-columns: 250px 1fr 300px; min-height: 0;
}
.col { display: flex; flex-direction: column; gap: 4px; min-height: 0; }
.panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
  padding: 10px 12px; display: flex; flex-direction: column; min-height: 0;
}
.panel.grow { flex: 1; }
.panel h3 { margin: 0 0 8px; font-size: 12px; color: var(--dim); text-transform: uppercase; }
.kv div, .bt-result div { display: flex; justify-content: space-between; padding: 2px 0; }
.kv span, .bt-result span { color: var(--dim); }
.scroll { overflow-y: auto; min-height: 0; flex: 1; }
.row {
  display: flex; gap: 8px; align-items: baseline; padding: 3px 0;
  border-bottom: 1px solid rgba(255,255,255,0.04); white-space: nowrap;
}
.reason { overflow: hidden; text-overflow: ellipsis; }

.account-panel .equity-line { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.equity-num { font-size: 20px; }
.spark { flex: 1; height: 28px; }

.center { min-width: 0; }
.chart-box {
  flex: 1; background: var(--panel); border: 1px solid var(--border);
  border-radius: 4px; overflow: hidden; min-height: 0;
}
.statusline {
  display: flex; justify-content: space-between; padding: 4px 8px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
}

.tabs { display: flex; gap: 4px; }
.tabs button {
  flex: 1; background: var(--panel); border: 1px solid var(--border); color: var(--dim);
  padding: 7px 0; border-radius: 4px; cursor: pointer; font-size: 13px;
}
.tabs button.active { color: var(--accent); border-color: var(--accent); }

.side-pick { display: flex; gap: 6px; margin-bottom: 10px; }
.side-btn {
  flex: 1; padding: 8px 0; border-radius: 4px; cursor: pointer; font-weight: 600;
  background: var(--bg); border: 1px solid var(--border); color: var(--dim);
}
.side-btn.long.active { background: rgba(14,203,129,0.15); color: var(--up); border-color: var(--up); }
.side-btn.short.active { background: rgba(246,70,93,0.15); color: var(--down); border-color: var(--down); }
.ticket label { display: flex; flex-direction: column; gap: 4px; margin-bottom: 8px; color: var(--dim); }
.ticket input[type=number] { width: 100%; }
.slider { width: 100%; accent-color: var(--accent); }
.ticket-meta { font-size: 11px; margin-bottom: 10px; }
.btn.submit { width: 100%; padding: 10px 0; font-weight: 700; font-size: 14px; }
.btn.submit.long { background: var(--up); color: #0b0e11; }
.btn.submit.short { background: var(--down); color: #fff; }

.pos-card {
  border: 1px solid var(--border); border-radius: 4px; padding: 8px;
  margin-bottom: 8px; background: var(--bg);
}
.pos-head { display: flex; gap: 8px; align-items: baseline; margin-bottom: 4px; }
.pos-head b:last-child { margin-left: auto; }
.pos-meta { font-size: 11px; }
.close-btn { width: 100%; margin-top: 6px; padding: 5px 0; }

.bt-form { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 6px; }
.bt-form input { width: 52px; }
.bt-result .verdict { margin-top: 6px; font-weight: 600; }

.auto-panel { border-color: var(--border); }
.auto-panel.live { border-color: var(--accent); }
.auto-head { display: flex; justify-content: space-between; align-items: center; }
.auto-head h3 { margin: 0; }
.auto-toggle.on { background: var(--down); color: #fff; }
.auto-toggle:not(.on) { background: var(--up); color: #0b0e11; font-weight: 600; }
.auto-meta { font-size: 11px; margin-top: 6px; }
.auto-err { font-size: 11px; margin-top: 6px; color: var(--down); }
.auto-params { display: flex; gap: 8px; margin-top: 8px; }
.auto-params label { display: flex; flex-direction: column; gap: 3px; color: var(--dim); font-size: 11px; }
.auto-params input {
  background: var(--bg); border: 1px solid var(--border); color: var(--text);
  padding: 5px 8px; border-radius: 4px; width: 90px; outline: none;
}

/* --- trade ticket extras ------------------------------------------------- */
.ref-price {
  display: flex; align-items: baseline; gap: 6px; margin-bottom: 8px;
  padding: 5px 8px; background: var(--bg); border: 1px solid var(--border);
  border-radius: 4px; font-size: 12px;
}
.ref-price b { font-size: 14px; }
.chips { display: flex; gap: 4px; align-items: center; margin: -4px 0 8px; }
.chip {
  background: var(--bg); border: 1px solid var(--border); color: var(--dim);
  font-size: 11px; border-radius: 3px; padding: 2px 8px; cursor: pointer;
}
.chip:hover { color: var(--text); border-color: #3a424c; }
.chip.on { color: var(--accent); border-color: var(--accent); }
.chips-note { font-size: 11px; margin-left: auto; }

/* --- order history -------------------------------------------------------- */
.hist-filters {
  display: flex; gap: 8px; align-items: center; margin-bottom: 8px;
}
.hist-filters .bar-select { width: 92px; }
.chk { display: flex; gap: 4px; align-items: center; font-size: 12px; cursor: pointer; }
.mini-refresh { margin-left: auto; font-size: 11px; }
.hist-summary {
  display: flex; gap: 14px; font-size: 12px; color: var(--dim);
  padding: 5px 8px; margin-bottom: 8px;
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
}
.order-card {
  border: 1px solid var(--border); border-radius: 4px;
  padding: 6px 8px; margin-bottom: 6px; background: var(--bg);
}
.or-line1 { display: flex; gap: 8px; align-items: baseline; }
.or-line1 .or-time { margin-left: auto; font-size: 11px; }
.or-line2 { font-size: 11px; margin-top: 2px; }
</style>
