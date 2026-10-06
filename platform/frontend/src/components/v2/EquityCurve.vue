<script setup>
/**
 * 账户权益曲线。
 * 原版用 100×28 的 SVG polyline——只能表达"涨了还是跌了"，
 * 换不出下跌的深度、能看出最大回撤。这里给出带坐标轴的正经曲线。
 *
 * 一个刻意的取舍：数据点超过 150 时降采样为按时间分桶的收盘价，
 * 而不是画 500 个点。500 个点在 300px 宽的卡片里挤成一团黑线，
 * 反而看不见形状。降采样在视觉上是无损的（相邻点连线几乎重合）。
 */
import { computed, ref } from 'vue'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  height: { type: Number, default: 128 },
})

const hover = ref(null) // 悬停点索引，用于 tooltip

// 降采样：桶数按容器可用点数决定（每点至少 3px 间距）
const MAX_POINTS = 90
const series = computed(() => {
  const raw = props.rows
  if (raw.length < 2) return []
  if (raw.length <= MAX_POINTS) return raw
  const bucketSize = Math.ceil(raw.length / MAX_POINTS)
  const out = []
  for (let i = 0; i < raw.length; i += bucketSize) {
    const bucket = raw.slice(i, i + bucketSize)
    // 取桶内最后一个值：代表"此刻"的权益，而不是桶内均值
    out.push(bucket[bucket.length - 1])
  }
  if (out[out.length - 1] !== raw[raw.length - 1]) out.push(raw[raw.length - 1])
  return out
})

const W = 300 // viewBox 宽，实际尺寸由 CSS 拉伸
const PAD_TOP = 8
const PAD_BOT = 16

const scale = computed(() => {
  const vals = series.value.map(p => p.equity)
  if (!vals.length) return null
  const lo = Math.min(...vals)
  const hi = Math.max(...vals)
  // 上下各留 8% 余量，避免曲线贴边（贴边的线视觉上像被裁掉）
  const span = (hi - lo) || Math.abs(hi) * 0.02 || 1
  const pad = span * 0.08
  const min = lo - pad
  const max = hi + pad
  const H = props.height
  return {
    min, max,
    x: i => (i / (series.value.length - 1)) * W,
    y: v => PAD_TOP + (1 - (v - min) / (max - min)) * (H - PAD_TOP - PAD_BOT),
    H,
  }
})

const linePath = computed(() => {
  if (!scale.value || series.value.length < 2) return ''
  return series.value
    .map((p, i) => `${i === 0 ? 'M' : 'L'}${scale.value.x(i).toFixed(2)},${scale.value.y(p.equity).toFixed(2)}`)
    .join(' ')
})

// 面积填充路径：从曲线闭合到基线，让"净值低于起点"这类信息一眼可见
const areaPath = computed(() => {
  if (!linePath.value) return ''
  const s = scale.value
  const base = s.y(s.min)
  return `${linePath.value} L${W},${base.toFixed(2)} L0,${base.toFixed(2)} Z`
})

const isUp = computed(() => {
  if (series.value.length < 2) return true
  return series.value[series.value.length - 1].equity >= series.value[0].equity
})
const stroke = computed(() => (isUp.value ? 'var(--c-up)' : 'var(--c-down)'))
const fillTop = computed(() => (isUp.value ? 'var(--c-up-soft)' : 'var(--c-down-soft)'))

const gridY = computed(() => {
  if (!scale.value) return []
  const s = scale.value
  return [s.min, s.min + (s.max - s.min) / 2, s.max].map(v => ({
    y: s.y(v), v,
  }))
})

function fmtUsd(v) {
  return v >= 0 ? '+' + v.toFixed(2) : v.toFixed(2)
}
function fmtAxis(v) {
  return v >= 10000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(0)
}
function fmtTime(ts) {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function onMove(e) {
  if (!scale.value || !series.value.length) return
  const rect = e.currentTarget.getBoundingClientRect()
  const ratio = (e.clientX - rect.left) / rect.width
  const idx = Math.round(ratio * (series.value.length - 1))
  if (idx < 0 || idx >= series.value.length) { hover.value = null; return }
  const p = series.value[idx]
  hover.value = {
    x: scale.value.x(idx) / W * 100,
    y: scale.value.y(p.equity) / scale.value.H * 100,
    point: p,
    first: series.value[0],
  }
}
</script>

<template>
  <div class="equity" @mouseleave="hover = null">
    <svg v-if="series.length >= 2"
         class="equity__svg"
         :viewBox="`0 0 ${W} ${height}`"
         preserveAspectRatio="none"
         role="img"
         :aria-label="`权益曲线，${isUp ? '上升' : '下降'}趋势`"
         @mousemove="onMove">
      <defs>
        <linearGradient :id="isUp ? 'eqGradUp' : 'eqGradDown'" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" :stop-color="fillTop" />
          <stop offset="100%" stop-color="transparent" />
        </linearGradient>
      </defs>

      <!-- 水平参考线 + 右侧刻度 -->
      <g class="equity__grid">
        <template v-for="g in gridY" :key="g.y">
          <line :x1="0" :x2="W" :y1="g.y" :y2="g.y" />
          <text :x="W - 2" :y="g.y - 3" text-anchor="end">{{ fmtAxis(g.v) }}</text>
        </template>
      </g>

      <path :d="areaPath" :fill="`url(#${isUp ? 'eqGradUp' : 'eqGradDown'})`" />
      <path :d="linePath" fill="none" :stroke="stroke" stroke-width="1.6"
            stroke-linejoin="round" stroke-linecap="round"
            vector-effect="non-scaling-stroke" />

      <!-- 悬停指示：竖线 + 空心点，不遮挡曲线本身 -->
      <g v-if="hover">
        <line :x1="(hover.x / 100) * W" :x2="(hover.x / 100) * W"
              :y1="PAD_TOP" :y2="height - PAD_BOT" class="equity__cursor" />
        <circle :cx="(hover.x / 100) * W" :cy="(hover.y / 100) * height"
                r="3" :stroke="stroke" stroke-width="1.6" fill="var(--bg-base)" />
      </g>
    </svg>

    <div v-else class="equity__empty">权益数据不足，无法绘制曲线</div>

    <!-- tooltip 用 HTML 而非 SVG <foreignObject>：Safari 支持不稳 -->
    <div v-if="hover" class="equity__tip"
         :style="{ left: hover.x + '%', top: hover.y + '%' }">
      <div class="equity__tip-time">{{ fmtTime(hover.point.ts || hover.point.created_at) }}</div>
      <div class="num equity__tip-val">{{ fmtUsd(hover.point.equity) }}</div>
      <div v-if="hover.first" class="equity__tip-delta num"
           :class="hover.point.equity >= hover.first.equity ? 'tone-up' : 'tone-down'">
        {{ fmtUsd(hover.point.equity - hover.first.equity) }} 自起点
      </div>
    </div>
  </div>
</template>

<style scoped>
.equity {
  position: relative;
  width: 100%;
  min-height: 0;
}

.equity__svg {
  display: block;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.equity__grid line {
  stroke: var(--line-soft);
  stroke-width: 1;
  vector-effect: non-scaling-stroke;
}
.equity__grid text {
  fill: var(--tx-tertiary);
  font-family: var(--ff-num);
  font-size: 8px;
}

.equity__cursor {
  stroke: var(--line-strong);
  stroke-width: 1;
  stroke-dasharray: 2 2;
  vector-effect: non-scaling-stroke;
}

.equity__empty {
  display: grid;
  place-items: center;
  min-height: 60px;
  font-size: var(--fs-xs);
  color: var(--tx-tertiary);
}

.equity__tip {
  position: absolute;
  transform: translate(-50%, calc(-100% - 10px));
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-overlay);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  box-shadow: var(--sh-md);
  font-size: var(--fs-2xs);
  white-space: nowrap;
  pointer-events: none;
  z-index: var(--z-dropdown);
}
.equity__tip-time { color: var(--tx-tertiary); }
.equity__tip-val {
  font-size: var(--fs-sm);
  font-weight: var(--fw-bold);
  margin-top: 1px;
}
.equity__tip-delta { font-size: var(--fs-2xs); }
</style>