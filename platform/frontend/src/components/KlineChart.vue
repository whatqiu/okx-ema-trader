<script setup>
// Self-drawn canvas K-line with a real viewport: wheel-zoom, drag-pan,
// double-click reset. Zero chart libraries on purpose: a canvas is a few
// hundred lines, has no version to break, and draws 5000 bars without
// breaking a sweat.
//
// Colour convention: green up / red down — the exchange convention (OKX,
// Binance), matching the "real trading interface" brief. Flip these two
// constants if you prefer the Chinese A-share convention (red up).
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const UP = '#0ecb81'
const DOWN = '#f6465d'
const GRID = 'rgba(255,255,255,0.06)'
const TEXT = '#848e9c'
const EMA_FAST_COLOR = '#f0b90b'
const EMA_SLOW_COLOR = '#7b9fff'

const props = defineProps({
  rows: { type: Array, default: () => [] },       // [[ts,o,h,l,c,vol,confirm], ...] oldest first
  emaFast: { type: Number, default: 9 },
  emaSlow: { type: Number, default: 26 },
  markers: { type: Array, default: () => [] },     // [{ts, side, price}]
})

const canvasRef = ref(null)
const hover = ref(null)   // {x, y} in canvas CSS pixels
let ctx = null
let dpr = 1
let resizeObserver = null

// --- viewport: [viewEnd - viewSpan, viewEnd) indexes into rows -------------
const DEFAULT_SPAN = 120
const MIN_SPAN = 15
const viewSpan = ref(DEFAULT_SPAN)
const viewEnd = ref(0)          // 0 = "follow the newest bar"
const following = computed(() => viewEnd.value === 0)
let drag = null                 // {startX, startEnd} while panning

// Layout fractions of total height.
const PRICE_H = 0.68
const VOL_H = 0.17
const AXIS_H = 0.15
const PAD_R = 64   // right gutter for price labels
const PAD_L = 8

function emaSeries(values, period) {
  if (!values.length) return []
  const k = 2 / (period + 1)
  const out = new Array(values.length).fill(null)
  let prev = values[0]
  out[0] = prev
  for (let i = 1; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k)
    out[i] = i >= period - 1 ? prev : null
  }
  return out
}

const emaFastArr = computed(() =>
  emaSeries(props.rows.map(r => r[4]), props.emaFast))
const emaSlowArr = computed(() =>
  emaSeries(props.rows.map(r => r[4]), props.emaSlow))

const lastPrice = computed(() =>
  props.rows.length ? props.rows[props.rows.length - 1][4] : null)

const lastChangePct = computed(() => {
  if (props.rows.length < 2) return null
  const prev = props.rows[props.rows.length - 2][4]
  return prev ? (lastPrice.value - prev) / prev * 100 : null
})

// Resolved visible window. end===0 means "pinned to the newest bar".
const view = computed(() => {
  const n = props.rows.length
  const end = following.value ? n : Math.min(viewEnd.value, n)
  const span = Math.min(viewSpan.value, Math.max(n, 1))
  return { start: Math.max(0, end - span), end, n }
})

function resetView() {
  viewSpan.value = DEFAULT_SPAN
  viewEnd.value = 0
  draw()
}

function onWheel(e) {
  e.preventDefault()
  if (!props.rows.length) return
  const v = view.value
  const rect = canvasRef.value.getBoundingClientRect()
  // Zoom anchored on the bar under the cursor, like every real chart does.
  const frac = Math.min(1, Math.max(0, (e.clientX - rect.left - PAD_L) / (rect.width - PAD_L - PAD_R)))
  const anchor = v.start + frac * (v.end - v.start)
  const factor = e.deltaY > 0 ? 1.15 : 1 / 1.15
  const oldSpan = v.end - v.start
  const newSpan = Math.min(props.rows.length,
                           Math.max(MIN_SPAN, Math.round(oldSpan * factor)))
  if (newSpan === oldSpan) return
  viewSpan.value = newSpan
  const newStart = Math.round(anchor - frac * newSpan)
  const newEnd = newStart + newSpan
  // Keep "following" only if we were already pinned to the right edge.
  viewEnd.value = (following.value && newEnd >= v.n) ? 0 : newEnd
  draw()
}

function onDown(e) {
  if (!props.rows.length) return
  const v = view.value
  drag = { startX: e.clientX, startEnd: v.end }
}
function onDragMove(e) {
  if (!drag) return
  const rect = canvasRef.value.getBoundingClientRect()
  const plotW = rect.width - PAD_L - PAD_R
  const v = view.value
  const perPx = (v.end - v.start) / plotW
  const deltaBars = Math.round((e.clientX - drag.startX) * perPx)
  const newEnd = drag.startEnd - deltaBars
  if (newEnd >= v.n) {
    viewEnd.value = 0                      // snapped back to the live edge
  } else {
    viewEnd.value = Math.max(viewSpan.value, newEnd)
  }
  draw()
}
function onUp() { drag = null }

function draw() {
  const canvas = canvasRef.value
  if (!canvas || !ctx) return
  const W = canvas.clientWidth
  const H = canvas.clientHeight
  ctx.clearRect(0, 0, W, H)

  const rows = props.rows
  if (!rows.length) {
    ctx.fillStyle = TEXT
    ctx.font = '13px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText('暂无K线数据 — 等待后端从 OKX 同步…', W / 2, H / 2)
    return
  }

  const { start, end, n } = view.value
  const slice = rows.slice(start, end)
  const count = slice.length
  if (!count) return

  const plotW = W - PAD_L - PAD_R
  const priceH = H * PRICE_H
  const volTop = priceH + H * 0.02
  const volH = H * VOL_H
  const slot = plotW / count
  const bodyW = Math.max(1, Math.min(slot * 0.7, 18))

  let hi = -Infinity, lo = Infinity, maxVol = 0
  for (const r of slice) {
    if (r[2] > hi) hi = r[2]
    if (r[3] < lo) lo = r[3]
    if (r[5] > maxVol) maxVol = r[5]
  }
  if (!(hi > lo)) { hi = lo + 1e-9 }
  const pad = (hi - lo) * 0.05
  hi += pad; lo -= pad

  const x = i => PAD_L + slot * (i + 0.5)          // i is index WITHIN the slice
  const y = p => priceH * (1 - (p - lo) / (hi - lo))

  // --- grid + price labels -------------------------------------------------
  ctx.font = '11px sans-serif'
  ctx.textAlign = 'left'
  const ticks = 6
  for (let t = 0; t <= ticks; t++) {
    const p = lo + (hi - lo) * t / ticks
    const yy = y(p)
    ctx.strokeStyle = GRID
    ctx.beginPath(); ctx.moveTo(PAD_L, yy); ctx.lineTo(W - PAD_R, yy); ctx.stroke()
    ctx.fillStyle = TEXT
    ctx.fillText(formatPrice(p), W - PAD_R + 6, yy + 3)
  }

  // --- volume ---------------------------------------------------------------
  for (let i = 0; i < count; i++) {
    const r = slice[i]
    const up = r[4] >= r[1]
    ctx.fillStyle = up ? UP + '55' : DOWN + '55'
    const vh = maxVol ? (r[5] / maxVol) * volH : 0
    ctx.fillRect(x(i) - bodyW / 2, volTop + volH - vh, bodyW, vh)
  }

  // --- candles ---------------------------------------------------------------
  for (let i = 0; i < count; i++) {
    const r = slice[i]
    const up = r[4] >= r[1]
    const color = up ? UP : DOWN
    const cx = x(i)
    ctx.strokeStyle = color
    ctx.beginPath()
    ctx.moveTo(cx, y(r[2])); ctx.lineTo(cx, y(r[3]))
    ctx.stroke()
    ctx.fillStyle = color
    const top = y(Math.max(r[1], r[4]))
    const bot = y(Math.min(r[1], r[4]))
    ctx.globalAlpha = r[6] === 0 ? 0.55 : 1   // forming bar is translucent
    ctx.fillRect(cx - bodyW / 2, top, bodyW, Math.max(1, bot - top))
    ctx.globalAlpha = 1
  }

  // --- EMA overlays (computed on full history, drawn for the window) --------
  for (const [arr, color] of [[emaFastArr.value, EMA_FAST_COLOR],
                              [emaSlowArr.value, EMA_SLOW_COLOR]]) {
    ctx.strokeStyle = color
    ctx.lineWidth = 1.2
    ctx.beginPath()
    let started = false
    for (let i = 0; i < count; i++) {
      const v = arr[start + i]
      if (v == null) continue
      const yy = y(v)
      if (!started) { ctx.moveTo(x(i), yy); started = true }
      else ctx.lineTo(x(i), yy)
    }
    ctx.stroke()
    ctx.lineWidth = 1
  }

  // --- signal markers ----------------------------------------------------------
  const tsToIndex = new Map(rows.map((r, i) => [r[0], i]))
  for (const m of props.markers) {
    let idx = tsToIndex.get(m.ts)
    if (idx == null) {
      let best = 0, bestDist = Infinity
      for (let i = 0; i < n; i++) {
        const d = Math.abs(rows[i][0] - m.ts)
        if (d < bestDist) { bestDist = d; best = i }
      }
      idx = best
    }
    if (idx < start || idx >= end) continue   // outside the visible window
    const r = rows[idx]
    const long = m.side !== 'short'
    const cx = x(idx - start)
    const cy = long ? y(r[3]) - 10 : y(r[2]) + 10
    ctx.fillStyle = long ? UP : DOWN
    ctx.beginPath()
    if (long) { ctx.moveTo(cx, cy - 6); ctx.lineTo(cx - 5, cy + 2); ctx.lineTo(cx + 5, cy + 2) }
    else { ctx.moveTo(cx, cy + 6); ctx.lineTo(cx - 5, cy - 2); ctx.lineTo(cx + 5, cy - 2) }
    ctx.closePath(); ctx.fill()
  }

  // --- last price line ---------------------------------------------------------
  if (lastPrice.value != null && lastPrice.value >= lo && lastPrice.value <= hi) {
    const yy = y(lastPrice.value)
    const color = (lastChangePct.value ?? 0) >= 0 ? UP : DOWN
    ctx.strokeStyle = color
    ctx.setLineDash([4, 4])
    ctx.beginPath(); ctx.moveTo(PAD_L, yy); ctx.lineTo(W - PAD_R, yy); ctx.stroke()
    ctx.setLineDash([])
    ctx.fillStyle = color
    const label = formatPrice(lastPrice.value)
    ctx.fillRect(W - PAD_R, yy - 9, PAD_R - 4, 16)
    ctx.fillStyle = '#0b0e11'
    ctx.fillText(label, W - PAD_R + 6, yy + 3)
  }

  // --- time axis ----------------------------------------------------------------
  ctx.fillStyle = TEXT
  ctx.textAlign = 'center'
  const labelEvery = Math.max(1, Math.floor(count / 6))
  for (let i = 0; i < count; i += labelEvery) {
    ctx.fillText(formatTime(slice[i][0]), x(i), H - H * AXIS_H / 2 + 4)
  }

  // --- crosshair -----------------------------------------------------------------
  if (hover.value && !drag) {
    const { x: mx, y: my } = hover.value
    const i = Math.round((mx - PAD_L) / slot - 0.5)
    if (i >= 0 && i < count && my < priceH + volH + 8) {
      const r = slice[i]
      ctx.strokeStyle = 'rgba(255,255,255,0.25)'
      ctx.setLineDash([3, 3])
      ctx.beginPath(); ctx.moveTo(x(i), 0); ctx.lineTo(x(i), volTop + volH); ctx.stroke()
      ctx.beginPath(); ctx.moveTo(PAD_L, my); ctx.lineTo(W - PAD_R, my); ctx.stroke()
      ctx.setLineDash([])
      const p = lo + (1 - my / priceH) * (hi - lo)
      if (my <= priceH) {
        ctx.fillStyle = '#2b3139'
        ctx.fillRect(W - PAD_R, my - 9, PAD_R - 4, 16)
        ctx.fillStyle = '#eaecef'
        ctx.textAlign = 'left'
        ctx.fillText(formatPrice(p), W - PAD_R + 6, my + 3)
      }
      const up = r[4] >= r[1]
      const c = up ? UP : DOWN
      ctx.font = '12px sans-serif'
      ctx.textAlign = 'left'
      const legend =
        `${formatTime(r[0])}  开 ${fmt(r[1])}  高 ${fmt(r[2])}  低 ${fmt(r[3])}  收 ${fmt(r[4])}` +
        `  涨跌 ${pct(r)}  量 ${fmtVol(r[5])}${r[6] === 0 ? '  (未收盘)' : ''}`
      ctx.fillStyle = c
      ctx.fillText(legend, PAD_L + 4, 14)
    }
  }
}

function fmt(v) { return formatPrice(v) }
function formatPrice(v) {
  if (v == null) return '-'
  if (v >= 1000) return v.toFixed(1)
  if (v >= 1) return v.toFixed(3)
  return v.toPrecision(4)
}
function pct(r) {
  return r[1] ? ((r[4] - r[1]) / r[1] * 100).toFixed(2) + '%' : '-'
}
function fmtVol(v) {
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M'
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K'
  return String(Math.round(v))
}
function formatTime(ts) {
  const d = new Date(ts)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  const hh = String(d.getHours()).padStart(2, '0')
  const mi = String(d.getMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${mi}`
}

function resize() {
  const canvas = canvasRef.value
  if (!canvas) return
  dpr = window.devicePixelRatio || 1
  canvas.width = canvas.clientWidth * dpr
  canvas.height = canvas.clientHeight * dpr
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  draw()
}

function onMove(e) {
  const rect = canvasRef.value.getBoundingClientRect()
  hover.value = { x: e.clientX - rect.left, y: e.clientY - rect.top }
  if (drag) onDragMove(e)
  else draw()
}
function onLeave() { hover.value = null; drag = null; draw() }

onMounted(() => {
  const canvas = canvasRef.value
  ctx = canvas.getContext('2d')
  resizeObserver = new ResizeObserver(resize)
  resizeObserver.observe(canvas)
  canvas.addEventListener('mousemove', onMove)
  canvas.addEventListener('mouseleave', onLeave)
  canvas.addEventListener('mousedown', onDown)
  window.addEventListener('mouseup', onUp)
  canvas.addEventListener('wheel', onWheel, { passive: false })
  canvas.addEventListener('dblclick', resetView)
  resize()
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  window.removeEventListener('mouseup', onUp)
})
// New data must not yank a panned view back to the edge: only redraw.
watch(() => [props.rows, props.markers], draw, { deep: true })
</script>

<template>
  <div class="kline-wrap">
    <div class="kline-legend">
      <span class="ema-fast">EMA{{ emaFast }}</span>
      <span class="ema-slow">EMA{{ emaSlow }}</span>
      <span class="up">■ 涨</span><span class="down">■ 跌</span>
      <span class="hint">滚轮缩放 · 拖拽平移 · 双击复位</span>
    </div>
    <button v-if="!following" class="back-live" @click="resetView" title="回到最新">⇥</button>
    <canvas ref="canvasRef" class="kline-canvas"></canvas>
  </div>
</template>

<style scoped>
.kline-wrap { position: relative; width: 100%; height: 100%; }
.kline-canvas { width: 100%; height: 100%; display: block; cursor: crosshair; }
.kline-legend {
  position: absolute; top: 4px; right: 72px; z-index: 2;
  display: flex; gap: 12px; font-size: 11px; pointer-events: none;
}
.ema-fast { color: #f0b90b; }
.ema-slow { color: #7b9fff; }
.up { color: #0ecb81; }
.down { color: #f6465d; }
.hint { color: #565f6b; }
.back-live {
  position: absolute; right: 70px; bottom: 18%; z-index: 3;
  background: #2b3139; color: #eaecef; border: 1px solid #3a424c;
  border-radius: 4px; width: 28px; height: 28px; cursor: pointer; font-size: 15px;
}
.back-live:hover { background: #3a424c; }
</style>
