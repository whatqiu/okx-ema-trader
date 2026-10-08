<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from './api'
import KlineChart from './components/KlineChart.vue'
import SymbolPicker from './components/SymbolPicker.vue'
import EquityCurve from './components/v2/EquityCurve.vue'
import PositionCard from './components/v2/PositionCard.vue'
import SignalFeed from './components/v2/SignalFeed.vue'
import TradeTicket from './components/v2/TradeTicket.vue'

import './design/tokens.css'
import './design/components.css'
import './design/app.css'

const BARS = ['1m', '3m', '5m', '15m', '30m', '1H', '4H', '1D']
const BAR_MS = {
  '1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000,
  '30m': 1800000, '1H': 3600000, '4H': 14400000, '1D': 86400000,
}
const CHART_HISTORY_DAYS = 30

/**
 * 浏览器只保留当前周期 30 天的数据；5m 的 8,640 根在传输层受 5,000
 * 上限约束。请求、增量合并和 ticker 开新根共用这一函数，避免任意一层
 * 又把已经补齐的历史悄悄裁回旧的 500 根。
 */
function historyLimit(timeframe) {
  return Math.min(5000, Math.round(CHART_HISTORY_DAYS * 86400000 / BAR_MS[timeframe]))
}

const symbol = ref(localStorage.getItem('symbol') || 'MU')
const bar = ref(localStorage.getItem('bar') || '5m')
const rows = ref([])
const candleExtent = ref(null)
const ticker = ref(null)
const signals = ref([])
const signalLog = ref([])
const orders = ref([])
const backtests = ref([])
const stats = ref(null)
const equityRows = ref([])
const account = ref(null)
const lastUpdate = ref(null)
const autoRefresh = ref(localStorage.getItem('autoRefresh') !== '0')

// 主题：默认跟随系统，用户可覆盖并持久化。
// 深色是交易终端惯例，但浅色不是可选项——白天靠窗办公的人需要它。
const theme = ref(localStorage.getItem('theme') || 'dark')

const showSettings = ref(false)
const proxyInput = ref('')
const resolvedProxy = ref('')

// --- 右侧标签页：每个任务独立，不再共用一个 300px 栏 -------------------
const workTab = ref(localStorage.getItem('workTab') || 'trade')
const TABS = [
  { key: 'trade', label: '交易' },
  { key: 'auto', label: '自动' },
  { key: 'backtest', label: '回测' },
  { key: 'history', label: '历史' },
]
watch(workTab, t => {
  localStorage.setItem('workTab', t)
  if (t === 'history') loadOrders()
})

// --- 交易 -----------------------------------------------------------------
const tradeSide = ref('long')
const tradeNotional = ref(100)
const tradeLeverage = ref(5)
const trading = ref(false)
const closingId = ref(null)

// --- 回测 -----------------------------------------------------------------
const btDays = ref(30)
const btFee = ref(5)
const btSlip = ref(3)
const btRunning = ref(false)
const btResult = ref(null)

// --- 自动交易 --------------------------------------------------------------
// 这三个初值是"显示占位"，不是配置。
// 真实配置一律以 /api/autotrade 返回值为准（见 loadPanels）——
// 曾在这里硬编码 100/5/3，而后端实际跑的是 3000/20/5，
// 结果是用户打开自动交易页看到的是从未生效过的数字，
// 改一个参数就可能把真实仓位规模改掉 30 倍。
const auto = ref(null)
const autoNotional = ref(0)
const autoLeverage = ref(1)
const autoMaxPositions = ref(1)
const autoSymbolInput = ref('')
const autoBusy = ref(false)

/**
 * 把后端配置同步进输入框。
 * 只在 auto 对象整体替换时执行，避免用户正在输入时被覆盖。
 */
function syncAutoFields(v) {
  if (!v) return
  if (Number.isFinite(v.notional)) autoNotional.value = v.notional
  if (Number.isFinite(v.leverage)) autoLeverage.value = v.leverage
  if (Number.isFinite(v.max_positions)) autoMaxPositions.value = v.max_positions
}

// --- 订单历史 --------------------------------------------------------------
const historyOrders = ref([])
const ordStatus = ref('')
const ordOnlyCurrent = ref(false)

// --- 消息提示：用 toast 替代横幅 -----------------------------------------
// 原版把 notice/error 做成顶部横幅，会把图表往下挤。瞬时反馈不该改变布局。
const toasts = ref([])
let toastSeq = 0

function pushToast(msg, kind = 'info', ttl = 4000) {
  const id = ++toastSeq
  toasts.value = [...toasts.value, { id, msg, kind }]
  setTimeout(() => {
    toasts.value = toasts.value.filter(t => t.id !== id)
  }, ttl)
}
const flash = msg => pushToast(msg, 'ok')

// --- 存活性 + 风控 ---------------------------------------------------------
const status = ref(null)
const wasOffline = ref(false)
const riskBusy = ref(false)

const connOk = computed(() => !!status.value?.connected)
const offlineFor = computed(() => status.value?.market?.offline_for_s ?? 0)
const losingStreak = computed(() => status.value?.risk?.losing_streak ?? 0)
const riskActive = computed(() => !!status.value?.risk?.limits?.enabled)
const riskHalted = computed(() => !!status.value?.risk?.halted)
const riskHaltReason = computed(() => status.value?.risk?.halt_reason || '')

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
      pushToast('网络已恢复', 'ok')
    }
    const closed = s?.risk?.last?.closed || []
    if (closed.length) {
      // 同一笔风控平仓只提示一次。轮询每 4s 一次，不去重的话
      // 同一条消息会反复刷屏直到用户手动关掉。
      const sig = JSON.stringify(closed.map(c => [c.id ?? c.order_id, c.reason]))
      if (sig !== lastRiskSig.value) {
        lastRiskSig.value = sig
        pushToast(`风控平仓：${closed[closed.length - 1].reason}`, 'warn', 8000)
      }
    }
  } catch {
    // 后端本身不可达。保留上一次状态，否则徽标会翻转成
    // "与OKX断线"——但问题其实是这个进程挂了。
    status.value = null
  }
}
const lastRiskSig = ref('')

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
let syncing = ref(false)

const changePct = computed(() => {
  const t = ticker.value
  if (!t || !t.open24h) return null
  return (t.last - t.open24h) / t.open24h * 100
})
const changeTone = computed(() =>
  changePct.value == null ? '' : changePct.value >= 0 ? 'tone-up' : 'tone-down')

const markers = computed(() => {
  const sig = signals.value
    .filter(s => s.side && s.price)
    .map(s => ({ ts: s.ts, side: s.side, price: s.price }))
  const trades = orders.value
    .filter(o => o.entry_price)
    .map(o => ({ ts: o.opened_at, side: o.side, price: o.entry_price }))
  return [...sig, ...trades]
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
function shortInst(inst) { return (inst || '').split('-')[0] || '-' }
function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
         `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function fmtClock(ts) {
  if (!ts) return '--:--:--'
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

/** 统一的异常出口：把后端返回的 detail 变成 toast，不改变布局。 */
async function guard(fn, silent = false) {
  try {
    await fn()
  } catch (e) {
    if (!silent) pushToast(e.message || '操作失败', 'bad', 6000)
  }
}

/**
 * 用时间戳覆盖合并增量：未收盘的最后一根会被服务端修正，新开的根则
 * 追加。空增量直接复用原数组，避免 KlineChart 在无行情变化时重绘。
 */
function mergeRows(base, incoming) {
  if (!incoming?.length) return base
  const byTimestamp = new Map(base.map(row => [row[0], row]))
  for (const row of incoming) byTimestamp.set(row[0], row)
  const merged = [...byTimestamp.values()].sort((left, right) => left[0] - right[0])
  const maxRows = historyLimit(bar.value)
  return merged.length > maxRows ? merged.slice(-maxRows) : merged
}

async function loadCandles(sync) {
  if (sync) {
    syncing.value = true
    candleExtent.value = null
  }
  await guard(async () => {
    let data
    const wasWarming = candleExtent.value?.warming === true
    if (sync) {
      data = await api.candles(
        symbol.value, bar.value, historyLimit(bar.value), true)
      rows.value = data.rows
    } else {
      const since = rows.value.length ? rows.value[rows.value.length - 1][0] : 0
      data = await api.candles(symbol.value, bar.value, 600, false, since)
      rows.value = mergeRows(rows.value, data.rows)
      // 增量响应故意只带最新几根，因此后台补入的更老历史不会通过
      // ``since`` 回来。预热完成的那个轮次只从本地完整读取一次，之后
      // 仍恢复小增量；没有这一步，状态会显示“补全”但图上永远是首批。
      if (wasWarming && !data.extent?.warming
          && rows.value.length < historyLimit(bar.value)) {
        data = await api.candles(
          symbol.value, bar.value, historyLimit(bar.value), false)
        rows.value = data.rows
      }
    }
    candleExtent.value = data.extent
    lastUpdate.value = Date.now()
  }, true)
  if (sync) syncing.value = false
}

async function loadTicker() {
  await guard(async () => {
    ticker.value = await api.ticker(symbol.value)
    mergeTickIntoCandle()
  }, true)
}

/**
 * 服务端只在K线收盘时抓取数据（省请求额度），当前未收盘的那根
 * 在前端用 ticker 补出来：每个 tick 更新收/高/低，跨过周期边界就开新根。
 * 视觉上和服务端刷新一致，且零额外请求。
 */
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
    // 上限必须跟着请求深度走；固定裁成 500 会让已经补到浏览器的
    // 30 天历史在每次 ticker 跨周期开新根时再次缩水。
    const excess = rows.value.length - historyLimit(bar.value)
    if (excess > 0) rows.value.splice(0, excess)
  }
  rows.value = rows.value.slice()  // 嵌套数组变更需手动触发响应式
}

async function loadPanels() {
  await guard(async () => {
    const [s, o, b, st, eq, acc, slog] = await Promise.all([
      api.signals(symbol.value, 50), api.orders({ symbol: symbol.value, limit: 50 }),
      api.backtests(symbol.value, 10), api.stats(),
      api.equity(300), api.account(),
      // 不过滤：扫描器监控多个币种，按图表币种过滤会隐藏其他币的信号
      api.signals(null, 200),
    ])
    signals.value = s.rows
    signalLog.value = slog.rows
    orders.value = o.rows
    backtests.value = b.rows
    stats.value = st
    equityRows.value = eq.rows
    account.value = acc
    const at = await api.autotrade()
    auto.value = at
    syncAutoFields(at)
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

async function applySymbol() {
  localStorage.setItem('symbol', symbol.value)
  localStorage.setItem('bar', bar.value)
  await loadCandles(true)
  await Promise.all([loadTicker(), loadPanels()])
  if (workTab.value === 'history') await loadOrders()
}

// 市价单的实际成交价：多头吃卖一，空头吃买一。
// 让用户在点下按钮前就知道成交价不是K线上的中间价。
const refPrice = computed(() => {
  const t = ticker.value
  if (!t) return null
  return tradeSide.value === 'long' ? t.ask : t.bid
})

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
  // 原版用 window.confirm——它会阻塞整个渲染，且在部分内嵌浏览器里被静默禁用，
  // 表现为"点了没反应"。这里用自定义弹窗。
  if (!confirmReset.value) { confirmReset.value = true; return }
  confirmReset.value = false
  await guard(async () => {
    await api.resetAccount(10000)
    flash('账户已重置为 10,000 USDT')
    await loadPanels()
  })
}
const confirmReset = ref(false)
watch(confirmReset, v => { if (v) setTimeout(() => { confirmReset.value = false }, 6000) })

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

function scanOf(inst) { return auto.value?.scan?.[inst] || null }

async function addAutoSymbol(raw) {
  const name = (raw || '').trim()
  if (!name) return
  const current = [...(auto.value?.symbols || [])]
  if (current.includes(name) || current.includes(name.toUpperCase())) {
    flash('该币种已在监控列表中'); return
  }
  autoBusy.value = true
  await guard(async () => {
    // 这里的 auto.value 赋值只用于立刻刷新币种列表；
    // 金额/杠杆/持仓数由后面的 loadPanels() → syncAutoFields() 同步。
    // 不要在这里调 syncAutoFields：那会盖掉用户正在输入的内容。
    auto.value = await api.setAutotrade({ symbols: [...current, name] })
    autoSymbolInput.value = ''
    flash(`已加入监控：${auto.value.symbols.join('、')}`)
    await loadPanels()
  })
  autoBusy.value = false
}

async function removeAutoSymbol(inst) {
  const rest = (auto.value?.symbols || []).filter(s => s !== inst)
  autoBusy.value = true
  await guard(async () => {
    auto.value = await api.setAutotrade({ symbols: rest })
    flash(`已移出监控：${inst}`)
    await loadPanels()
  })
  autoBusy.value = false
}

async function toggleAutoTrade() {
  if (!auto.value) return
  if (!auto.value.enabled && !(auto.value.symbols || []).length) {
    flash('请先添加至少一个监控币种'); return
  }
  autoBusy.value = true
  await guard(async () => {
    auto.value = await api.setAutotrade({
      enabled: !auto.value.enabled,
      symbols: auto.value.symbols,
      notional: Number(autoNotional.value),
      leverage: Number(autoLeverage.value),
      max_positions: Number(autoMaxPositions.value),
    })
    flash(auto.value.enabled
      ? `自动交易已开启：${auto.value.symbols.join('、')} · 每根5m收盘评估信号`
      : '自动交易已停止（已有持仓不会自动平仓）')
    await loadPanels()
  })
  autoBusy.value = false
}

async function resetRisk() {
  riskBusy.value = true
  await guard(async () => {
    await api.riskReset()
    await loadStatus()
    flash('熔断已解除，连亏计数从现在起重算')
  })
  riskBusy.value = false
}

async function saveAutoParams() {
  autoBusy.value = true
  await guard(async () => {
    auto.value = await api.setAutotrade({
      notional: Number(autoNotional.value),
      leverage: Number(autoLeverage.value),
      max_positions: Number(autoMaxPositions.value),
    })
    flash('自动交易参数已保存')
  })
  autoBusy.value = false
}

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

/** 主题写入 DOM 属性，供 tokens.css 的 [data-theme='light'] 选择器接管。 */
function applyTheme() {
  document.documentElement.setAttribute('data-theme', theme.value)
  localStorage.setItem('theme', theme.value)
}
function toggleTheme() {
  theme.value = theme.value === 'light' ? 'dark' : 'light'
  applyTheme()
}

function setupTimers() {
  timers.forEach(clearInterval)
  timers = autoRefresh.value ? [
    setInterval(() => loadCandles(false), 5000),
    setInterval(loadTicker, 3000),
    setInterval(loadPanels, 10000),
    // 存活性故意不绑在自动刷新开关上。关掉刷新 = "别再向市场要K线"，
    // 但你依然需要知道连接是否断了。
    setInterval(loadStatus, 4000),
  ] : [
    setInterval(loadStatus, 4000),
  ]
}

onMounted(async () => {
  applyTheme()
  await applySymbol()
  await Promise.all([loadSettings(), loadStatus()])
  setupTimers()
})
onBeforeUnmount(() => timers.forEach(clearInterval))
</script>

<template>
  <div class="app">
    <!-- ================= 顶部栏 ================= -->
    <header class="topbar">
      <div class="brand">
        <span class="brand__mark">OKX</span>
        <span class="brand__text">EMA Trader</span>
        <span class="tag tag--warn">模拟盘</span>
      </div>

      <div class="topbar__instrument">
        <SymbolPicker v-model="symbol" @select="applySymbol" />
        <select v-model="bar" class="select select--bar" aria-label="K线周期" @change="applySymbol">
          <option v-for="b in BARS" :key="b" :value="b">{{ b }}</option>
        </select>
      </div>

      <!-- 当前价格：最大字号的信息，视线落点 -->
      <div v-if="ticker" class="quote">
        <span class="quote__last num" :class="changeTone">{{ fmtNum(ticker.last) }}</span>
        <span class="quote__chg num" :class="changeTone">
          {{ changePct == null ? '—' : (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + '%' }}
        </span>
      </div>

      <div class="topbar__status">
        <!-- 三重编码：颜色 + 圆点 + 文字。仅靠颜色会让色觉障碍用户
             把"已连接"误读成"断网"，而这个判断错误的代价很高。 -->
        <span v-if="status" class="badge"
              :class="connOk ? 'badge--ok' : 'badge--bad'"
              :title="connOk
                ? `已连接 OKX，最后成功 ${lastOkText()}`
                : `断网 ${fmtDuration(offlineFor)}｜最后成功 ${lastOkText()}｜${status.market?.last_error || status.auto?.last_error || '未知原因'}`">
          <span class="badge__dot"></span>
          {{ connOk ? '已连接' : '断网' + fmtDuration(offlineFor) }}
        </span>

        <span v-if="status && riskActive" class="badge"
              :class="losingStreak > 0 ? 'badge--warn' : 'badge--idle'"
              :title="`风控：单仓浮亏 >${status.risk.limits.max_loss_pct}% 强平｜账户回撤 >${status.risk.limits.max_drawdown_pct}% 强平｜连亏 ${status.risk.limits.max_consecutive_losses} 次停手`">
          {{ riskHalted ? '风控熔断' : losingStreak > 0 ? `连亏${losingStreak}` : '风控启用' }}
        </span>

        <button class="btn btn--ghost btn--icon"
                :class="{ 'is-on': autoRefresh }"
                :title="autoRefresh ? '自动刷新：开' : '自动刷新：关'"
                :aria-pressed="autoRefresh"
                @click="toggleAuto">
          {{ autoRefresh ? '⟳' : '⏸' }}
        </button>
        <button v-if="!autoRefresh" class="btn btn--ghost" @click="applySymbol">刷新</button>

        <button class="btn btn--ghost btn--icon" :title="'切换到' + (theme === 'light' ? '深色' : '浅色') + '主题'"
                @click="toggleTheme">
          {{ theme === 'light' ? '☀' : '☾' }}
        </button>
        <button class="btn btn--ghost btn--icon" title="设置"
                :aria-expanded="showSettings" @click="showSettings = !showSettings">⚙</button>
      </div>
    </header>

    <!-- 断网横幅：持续显示时长 + 原因。
         断线不能看起来像"行情平静"——用户会据此做决策。 -->
    <div v-if="status && !connOk" class="banner banner--danger">
      <span class="banner__strong">已与 OKX 断线 {{ fmtDuration(offlineFor) }}</span>
      <span class="banner__detail">最后成功连接：{{ lastOkText() }}</span>
      <span class="banner__detail">{{ status.market?.last_error || status.auto?.last_error || '原因未知' }}</span>
      <span class="banner__detail">断线期间浮亏按开仓价估算；恢复后风控会立即重新判定。</span>
    </div>

    <!-- 熔断是唯一必须"解释而非仅展示"的状态：
         机器人看起来活着，但就是不开仓，只有��工能解除。 -->
    <div v-if="riskHalted" class="banner banner--danger">
      <span class="banner__strong">风控已熔断：{{ riskHaltReason || '未知原因' }}</span>
      <span class="banner__detail">已停止开新仓（平仓/减仓仍可用），需人工解除。</span>
      <button class="btn btn--ghost banner__spacer" :disabled="riskBusy" @click="resetRisk">
        {{ riskBusy ? '解除中…' : '解除熔断' }}
      </button>
    </div>

    <div v-if="showSettings" class="banner banner--info">
      <span class="banner__detail">代理（当前生效：{{ resolvedProxy }}）</span>
      <input v-model="proxyInput" class="input input--proxy"
             placeholder="留空 = 系统代理，如 http://127.0.0.1:6088" />
      <button class="btn" @click="saveProxy">保存</button>
      <button class="btn btn--ghost banner__spacer" @click="showSettings = false">收起</button>
    </div>

    <!-- ================= 主体 ================= -->
    <main class="layout">
      <!-- ---------- 左栏：账户 + 权益 + 信号 ---------- -->
      <aside class="layout__left">
        <section v-if="account" class="panel panel--pad">
          <h2 class="panel__title">
            模拟账户
            <button class="btn btn--ghost btn--mini"
                    :class="{ 'is-on': confirmReset }"
                    @click="doResetAccount">
              {{ confirmReset ? '确认重置？' : '重置' }}
            </button>
          </h2>

          <div class="equity-head">
            <div class="equity-head__figure">
              <span class="equity-head__k">总权益</span>
              <b class="equity-head__v num">{{ fmtUsdt(account.equity) }}</b>
              <span class="equity-head__unit">USDT</span>
            </div>
          </div>

          <EquityCurve :rows="equityRows" :height="88" />

          <dl class="kv kv--tight">
            <div class="kv__row">
              <dt class="kv__k">可用</dt>
              <dd class="kv__v">{{ fmtUsdt(account.available) }}</dd>
            </div>
            <div class="kv__row">
              <dt class="kv__k">已用保证金</dt>
              <dd class="kv__v">{{ fmtUsdt(account.margin_used) }}</dd>
            </div>
            <div class="kv__row">
              <dt class="kv__k">未实现盈亏</dt>
              <dd class="kv__v" :class="account.unrealised_pnl >= 0 ? 'tone-up' : 'tone-down'">
                {{ account.unrealised_pnl >= 0 ? '+' : '' }}{{ fmtUsdt(account.unrealised_pnl) }}
              </dd>
            </div>
            <div class="kv__row">
              <dt class="kv__k">已实现盈亏</dt>
              <dd class="kv__v" :class="account.realised_pnl >= 0 ? 'tone-up' : 'tone-down'">
                {{ account.realised_pnl >= 0 ? '+' : '' }}{{ fmtUsdt(account.realised_pnl) }}
              </dd>
            </div>
          </dl>
        </section>

        <!-- 24h 行情：横排而非竖排，纵向空间让给权益曲线和信号 -->
        <section v-if="ticker" class="panel panel--pad">
          <h2 class="panel__title">24 小时</h2>
          <div class="ticker-grid">
            <div class="ticker-grid__cell">
              <span class="ticker-grid__k">买一</span>
              <b class="num tone-up">{{ fmtNum(ticker.bid) }}</b>
            </div>
            <div class="ticker-grid__cell">
              <span class="ticker-grid__k">卖一</span>
              <b class="num tone-down">{{ fmtNum(ticker.ask) }}</b>
            </div>
            <div class="ticker-grid__cell">
              <span class="ticker-grid__k">24h 开</span>
              <b class="num">{{ fmtNum(ticker.open24h) }}</b>
            </div>
            <div class="ticker-grid__cell">
              <span class="ticker-grid__k">24h 额</span>
              <b class="num">{{ fmtNum(ticker.vol_ccy24h, 0) }}</b>
            </div>
            <div class="ticker-grid__cell">
              <span class="ticker-grid__k">24h 高</span>
              <b class="num">{{ fmtNum(ticker.high24h) }}</b>
            </div>
            <div class="ticker-grid__cell">
              <span class="ticker-grid__k">24h 低</span>
              <b class="num">{{ fmtNum(ticker.low24h) }}</b>
            </div>
          </div>
        </section>

        <section class="panel panel--fill">
          <h2 class="panel__title">
            信号记录
            <span class="panel__count">{{ signalLog.length }}</span>
          </h2>
          <div class="scroll panel__body">
            <SignalFeed :signals="signalLog" />
          </div>
        </section>
      </aside>

      <!-- ---------- 中栏：图表 ---------- -->
      <section class="layout__center">
        <div class="chart-card">
          <KlineChart :rows="rows" :markers="markers" :ema-fast="20" :ema-slow="50" />
        </div>
        <div class="statusline">
          <span class="statusline__item">
            <span :class="syncing ? 'tone-accent' : 'tone-dim'">
              {{ syncing
                ? '⟳ 正在从 OKX 补拉缺口…'
                : candleExtent?.warming
                  ? `⟳ 正在补全历史…（本地 ${rows.length} 根）`
                  : `本地 K 线 ${rows.length} 根` }}
            </span>
          </span>
          <span v-if="stats" class="statusline__item tone-dim num">
            库 {{ (stats.size_bytes / 1048576).toFixed(1) }} MB ·
            K线 {{ stats.counts.candles }} · 订单 {{ stats.counts.orders }} · 信号 {{ stats.counts.signals }}
          </span>
          <span class="statusline__item tone-dim num">更新 {{ fmtClock(lastUpdate) }}</span>
        </div>
      </section>

      <!-- ---------- 右栏：工作台（标签页隔离任务） ---------- -->
      <aside class="layout__right">
        <div class="tabs worktabs" role="tablist">
          <button v-for="t in TABS" :key="t.key"
                  class="tabs__btn"
                  :class="{ 'is-active': workTab === t.key }"
                  role="tab"
                  :aria-selected="workTab === t.key"
                  @click="workTab = t.key">{{ t.label }}</button>
        </div>

        <!-- ===== 交易 ===== -->
        <template v-if="workTab === 'trade'">
          <section class="panel panel--pad">
            <TradeTicket
              v-model:side="tradeSide"
              v-model:notional="tradeNotional"
              v-model:leverage="tradeLeverage"
              :ref-price="refPrice"
              :available="account?.available || 0"
              :busy="trading"
              @submit="openTrade"
              @set-pct="setNotionalPct" />
          </section>

          <section class="panel panel--fill">
            <h2 class="panel__title">
              持仓
              <span class="panel__count">{{ account?.positions?.length || 0 }}</span>
            </h2>
            <div class="scroll panel__body pos-list">
              <PositionCard
                v-for="p in account?.positions || []"
                :key="p.id"
                :position="p"
                :busy="closingId === p.id"
                @close="closeTrade" />
              <div v-if="!(account?.positions?.length)" class="empty">
                <span class="empty__icon">◇</span>
                <span>当前无持仓</span>
                <span>用上方交易台手动开仓，或在「自动」标签页启动策略</span>
              </div>
            </div>
          </section>
        </template>

        <!-- ===== 自动 ===== -->
        <template v-else-if="workTab === 'auto'">
          <section class="panel panel--pad" :class="{ 'panel--live': auto?.enabled }">
            <div class="auto-head">
              <h2 class="panel__title">
                自动交易
                <span v-if="auto?.enabled" class="badge badge--ok badge--pulse">
                  <span class="badge__dot"></span>运行中
                </span>
                <span v-else class="badge badge--idle">已停止</span>
              </h2>
              <button class="btn"
                      :class="auto?.enabled ? 'btn--danger' : 'btn--primary'"
                      :disabled="autoBusy" @click="toggleAutoTrade">
                {{ auto?.enabled ? '停止' : '启动' }}
              </button>
            </div>

            <p class="auto-desc">
              15m 趋势 + ADX&gt;20 + 5m EMA20/50 交叉 · 每根 5m 收盘评估 · 无信号不操作
            </p>

            <div class="field">
              <span class="field__label">监控币种 <span class="tone-dim">共 {{ auto?.symbols?.length || 0 }}</span></span>
              <div class="token-row">
                <span v-for="inst in (auto?.symbols || [])" :key="inst" class="token">
                  {{ shortInst(inst) }}
                  <button class="token__x" :disabled="autoBusy"
                          :title="`从监控列表移除 ${inst}`"
                          :aria-label="`移除 ${inst}`"
                          @click="removeAutoSymbol(inst)">×</button>
                </span>
                <span v-if="!(auto?.symbols || []).length" class="tone-dim auto-hint">
                  （未选择，请从下方添加）
                </span>
              </div>
              <div class="token-add">
                <input v-model="autoSymbolInput" class="input"
                       placeholder="如 BTC / ETH-USDT-SWAP"
                       @keyup.enter="addAutoSymbol(autoSymbolInput)" />
                <button class="btn" :disabled="autoBusy" @click="addAutoSymbol(autoSymbolInput)">添加</button>
                <button class="btn btn--ghost" :disabled="autoBusy" @click="addAutoSymbol(symbol)">加当前</button>
              </div>
            </div>

            <div class="auto-params">
              <label class="field">
                <span class="field__label">每笔 USDT</span>
                <input v-model.number="autoNotional" class="input input--num"
                       type="number" min="1" step="10" @change="saveAutoParams" />
              </label>
              <label class="field">
                <span class="field__label">杠杆</span>
                <input v-model.number="autoLeverage" class="input input--num"
                       type="number" min="1" max="20" @change="saveAutoParams" />
              </label>
              <label class="field">
                <span class="field__label">最大持仓</span>
                <input v-model.number="autoMaxPositions" class="input input--num"
                       type="number" min="1" max="10" @change="saveAutoParams" />
              </label>
            </div>

            <div v-if="auto?.last_action" class="auto-note">
              最近动作：{{ auto.last_action }}
            </div>
            <div v-if="auto?.last_error" class="auto-note auto-note--bad">
              最近错误：{{ auto.last_error }}
            </div>
          </section>

          <section v-if="(auto?.symbols || []).length" class="panel panel--fill">
            <h2 class="panel__title">扫描结果</h2>
            <div class="scroll panel__body">
              <div class="scan">
                <div v-for="inst in auto.symbols" :key="inst" class="scan__row">
                  <b class="scan__inst">{{ shortInst(inst) }}</b>
                  <span class="scan__hit"
                        :class="scanOf(inst)?.side === 'short' ? 'tone-down'
                               : scanOf(inst)?.side === 'long' ? 'tone-up' : 'tone-dim'">
                    {{ scanOf(inst)?.side === 'short' ? '看空'
                       : scanOf(inst)?.side === 'long' ? '看多' : '—' }}
                  </span>
                  <span class="scan__why">{{ scanOf(inst)?.why || '尚未评估' }}</span>
                </div>
              </div>
            </div>
          </section>
        </template>

        <!-- ===== 回测 ===== -->
        <template v-else-if="workTab === 'backtest'">
          <section class="panel panel--pad">
            <h2 class="panel__title">参数</h2>
            <div class="bt-params">
              <label class="field">
                <span class="field__label">天数</span>
                <input v-model.number="btDays" class="input input--num" type="number" min="1" max="365" />
              </label>
              <label class="field">
                <span class="field__label">费率 bps</span>
                <input v-model.number="btFee" class="input input--num" type="number" min="0" step="0.5" />
              </label>
              <label class="field">
                <span class="field__label">滑点 bps</span>
                <input v-model.number="btSlip" class="input input--num" type="number" min="0" step="0.5" />
              </label>
            </div>
            <button class="btn btn--primary btn--block btn--lg" :disabled="btRunning" @click="runBacktest">
              {{ btRunning ? '回测中…' : '运行回测' }}
            </button>
          </section>

          <section v-if="btResult" class="panel panel--pad">
            <h2 class="panel__title">
              本次结果
              <span class="tag" :class="btResult.net_return > 0 ? 'tag--up' : 'tag--down'">
                {{ btResult.net_return > 0 ? '正收益' : '负收益' }}
              </span>
            </h2>
            <dl class="kv">
              <div class="kv__row">
                <dt class="kv__k">净收益</dt>
                <dd class="kv__v" :class="btResult.net_return > 0 ? 'tone-up' : 'tone-down'">
                  {{ fmtPct(btResult.net_return) }}
                </dd>
              </div>
              <div class="kv__row">
                <dt class="kv__k">交易数 / 胜率</dt>
                <dd class="kv__v">{{ btResult.trades }} 笔 · {{ fmtPct(btResult.win_rate) }}</dd>
              </div>
              <div class="kv__row">
                <dt class="kv__k">账户最大回撤</dt>
                <dd class="kv__v tone-down">{{ fmtPct(btResult.max_drawdown_account) }}</dd>
              </div>
              <div class="kv__row">
                <dt class="kv__k">保本成本</dt>
                <dd class="kv__v">{{ btResult.breakeven_bps?.toFixed(1) }} bps</dd>
              </div>
            </dl>
            <p class="verdict" :class="btResult.net_return > 0 ? 'tone-up' : 'tone-down'">
              {{ btResult.verdict }}
            </p>
          </section>

          <section class="panel panel--fill">
            <h2 class="panel__title">
              历史回测
              <span class="panel__count">{{ backtests.length }}</span>
            </h2>
            <div class="scroll panel__body">
              <div v-for="b in backtests" :key="b.id" class="bt-row">
                <span class="num tone-dim">{{ fmtTime(b.created_at) }}</span>
                <span class="bt-row__days num">{{ b.days }}d</span>
                <b class="num" :class="b.result.net_return > 0 ? 'tone-up' : 'tone-down'">
                  {{ fmtPct(b.result.net_return) }}
                </b>
              </div>
              <div v-if="!backtests.length" class="empty">
                <span class="empty__icon">◷</span>
                <span>暂无回测记录</span>
              </div>
            </div>
          </section>
        </template>

        <!-- ===== 历史 ===== -->
        <template v-else>
          <section class="panel panel--fill">
            <h2 class="panel__title">
              订单记录
              <span class="panel__count">{{ historyOrders.length }}</span>
            </h2>

            <div class="filters">
              <select v-model="ordStatus" class="select" aria-label="订单状态" @change="loadOrders">
                <option value="">全部状态</option>
                <option value="open">持仓中</option>
                <option value="closed">已平仓</option>
              </select>
              <label class="check">
                <input type="checkbox" v-model="ordOnlyCurrent" @change="loadOrders" />
                仅当前币种
              </label>
            </div>

            <div v-if="orderSummary.closed" class="filters__summary">
              <span>已平 <b class="num">{{ orderSummary.closed }}</b> 笔</span>
              <span>胜率 <b class="num">{{ orderSummary.winRate == null ? '—' : (orderSummary.winRate * 100).toFixed(0) + '%' }}</b></span>
              <span>合计
                <b class="num" :class="orderSummary.total >= 0 ? 'tone-up' : 'tone-down'">
                  {{ orderSummary.total >= 0 ? '+' : '' }}{{ fmtUsdt(orderSummary.total) }}
                </b>
              </span>
            </div>

            <div class="scroll panel__body">
              <article v-for="o in historyOrders" :key="o.id" class="ord">
                <div class="ord__row">
                  <span class="tag" :class="o.side === 'short' ? 'tag--down' : 'tag--up'">
                    {{ o.side === 'short' ? '空' : '多' }}
                  </span>
                  <b class="ord__inst">{{ o.inst_id.replace('-USDT-SWAP', '') }}</b>
                  <span class="ord__lev num">{{ o.leverage }}x</span>
                  <b v-if="o.pnl != null" class="num ord__pnl" :class="o.pnl >= 0 ? 'tone-up' : 'tone-down'">
                    {{ o.pnl >= 0 ? '+' : '' }}{{ fmtUsdt(o.pnl) }}
                    <small v-if="o.pnl_pct != null">({{ (o.pnl_pct * 100).toFixed(1) }}%)</small>
                  </b>
                  <span v-else class="tag tag--warn">持仓中</span>
                </div>
                <div class="ord__row ord__row--sub num tone-dim">
                  {{ fmtNum(o.entry_price) }} → {{ o.exit_price ? fmtNum(o.exit_price) : '…' }}
                  · 名义 {{ fmtUsdt(o.notional) }}
                  <span class="ord__time">{{ fmtTime(o.opened_at) }}</span>
                </div>
              </article>
              <div v-if="!historyOrders.length" class="empty">
                <span class="empty__icon">☰</span>
                <span>暂无符合条件的订单</span>
              </div>
            </div>
          </section>
        </template>
      </aside>
    </main>

    <!-- Toast：瞬时反馈不改变布局 -->
    <div class="toast-stack" role="status" aria-live="polite">
      <div v-for="t in toasts" :key="t.id" class="toast" :class="'toast--' + t.kind">
        {{ t.msg }}
      </div>
    </div>
  </div>
</template>