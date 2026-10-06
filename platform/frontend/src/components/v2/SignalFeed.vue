<script setup>
/**
 * 信号流。
 *
 * 原版是一行纯文本，行内 7 列挤在 250px 里，reason 字段永远显示不全，
 * 且"是否成交"用颜色区分（已下单绿/未成交红）——把两个不同维度的状态
 * 混进同一个色彩通道，色觉障碍用户无法区分。
 *
 * 这里改成卡片式时间线：
 *   - 方向（多/空）用色块表示
 *   - 是否成交用文字标签，独立的色彩通道
 *   - reason 独占一行，可换行，不再被截断
 */
import { computed } from 'vue'

const props = defineProps({
  signals: { type: Array, default: () => [] },
})

const grouped = computed(() => {
  // 按日期分组：盯盘时"今天有没有信号"比"第 37 条是什么"更有用
  const buckets = []
  let cur = null
  for (const s of props.signals) {
    const d = new Date(s.ts)
    const key = `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
    if (!cur || cur.key !== key) {
      cur = { key, label: dateLabel(d), items: [] }
      buckets.push(cur)
    }
    cur.items.push(s)
  }
  return buckets
})

function dateLabel(d) {
  const today = new Date()
  const isToday = d.toDateString() === today.toDateString()
  const yest = new Date(today.getTime() - 86400000)
  const isYest = d.toDateString() === yest.toDateString()
  if (isToday) return '今天'
  if (isYest) return '昨天'
  return `${d.getMonth() + 1}月${d.getDate()}日`
}

function fmtTime(ts) {
  const d = new Date(ts)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
function fmtNum(v) {
  if (v == null || Number.isNaN(v)) return '-'
  if (Math.abs(v) >= 1000) return v.toFixed(1)
  if (Math.abs(v) >= 1) return v.toFixed(3)
  return Number(v).toPrecision(4)
}
function shortInst(inst) {
  return (inst || '').split('-')[0] || '-'
}
</script>

<template>
  <div class="feed">
    <template v-if="grouped.length">
      <section v-for="g in grouped" :key="g.key" class="feed__group">
        <div class="feed__day">
          <span>{{ g.label }}</span>
          <span class="feed__day-line"></span>
          <span class="num feed__day-n">{{ g.items.length }}</span>
        </div>

        <article
          v-for="s in g.items"
          :key="s.id"
          class="sig"
          :class="{ 'sig--long': s.side === 'long', 'sig--short': s.side === 'short' }"
        >
          <div class="sig__row">
            <time class="sig__time num">{{ fmtTime(s.ts) }}</time>
            <span class="sig__side">
              {{ s.side === 'short' ? '看空' : s.side === 'long' ? '看多' : '—' }}
            </span>
            <b class="sig__inst">{{ shortInst(s.inst_id) }}</b>
            <span class="num sig__price">{{ fmtNum(s.price) }}</span>
            <!-- 成交与否：文字标签独立承载，不依赖颜色 -->
            <span class="tag" :class="s.acted ? 'tag--info' : 'tag--muted'">
              {{ s.acted ? '已下单' : '未成交' }}
            </span>
          </div>
          <div class="sig__row sig__row--sub">
            <span v-if="s.adx != null" class="sig__metric num">ADX {{ Number(s.adx).toFixed(1) }}</span>
            <span v-if="s.reason" class="sig__reason">{{ s.reason }}</span>
          </div>
        </article>
      </section>
    </template>

    <div v-else class="empty">
      <span class="empty__icon">◎</span>
      <span>暂无信号记录</span>
      <span>自动交易每根 5m 收盘评估一次，出现方向判断才会写入这里</span>
    </div>
  </div>
</template>

<style scoped>
.feed { display: flex; flex-direction: column; gap: var(--sp-3); }

.feed__group { display: flex; flex-direction: column; gap: var(--sp-2); }

.feed__day {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  font-size: var(--fs-2xs);
  color: var(--tx-tertiary);
  position: sticky;
  top: 0;
  background: var(--bg-base);
  padding: var(--sp-1) 0;
  z-index: 1;
}
.feed__day-line { flex: 1; height: 1px; background: var(--line-soft); }
.feed__day-n { font-size: var(--fs-2xs); }

.sig {
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-raised);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-md);
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
  transition: border-color var(--dur-fast) var(--ease);
}
.sig:hover { border-color: var(--line); }

.sig__row { display: flex; align-items: center; gap: var(--sp-2); }

.sig__time { font-size: var(--fs-2xs); color: var(--tx-tertiary); }

.sig__side {
  font-size: var(--fs-2xs);
  font-weight: var(--fw-bold);
  min-width: 26px;
  padding: 1px 5px;
  border-radius: var(--r-sm);
  text-align: center;
}
.sig--long .sig__side { color: var(--c-up); background: var(--c-up-soft); }
.sig--short .sig__side { color: var(--c-down); background: var(--c-down-soft); }

.sig__inst { font-size: var(--fs-sm); font-weight: var(--fw-bold); }
.sig__price { font-size: var(--fs-xs); color: var(--tx-secondary); margin-left: auto; }

.sig__row--sub { gap: var(--sp-3); }
.sig__metric { font-size: var(--fs-2xs); color: var(--tx-tertiary); white-space: nowrap; }
.sig__reason {
  font-size: var(--fs-2xs);
  color: var(--tx-secondary);
  line-height: var(--lh-snug);
  /* 多行换行显示：原版单行截断导致关键判断依据不可见 */
  overflow-wrap: anywhere;
}
</style>