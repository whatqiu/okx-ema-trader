<script setup>
/**
 * 持仓卡片。
 * 原版持仓列表是一段 3 行文字，用户要在脑子里做减法才知道"离强平还有多远"。
 * 这里把三个关键量做成可视的：浮盈亏金额、距强平的缓冲比例、名义/保证金倍数。
 *
 * 强平缓冲是这��界面最该突出显示的数字——它决定用户会不会被强平，
 * 但原版把它埋在第二行灰色小字里。
 */
import { computed } from 'vue'

const props = defineProps({
  position: { type: Object, required: true },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits(['close'])

const isShort = computed(() => props.position.side === 'short')

const pnlTone = computed(() =>
  props.position.unrealised_pnl >= 0 ? 'tone-up' : 'tone-down')

/**
 * 强平缓冲：距离被强平还剩多少空间，占现价的百分比。
 *
 * 方向必须和持仓方向一致，否则会显示负数：
 *   多头爆仓要下跌，所以强平价在现价【下方】→ 空间 = (现价 - 强平价) / 现价
 *   空头爆仓要上涨，所以强平价在现价【上方】→ 空间 = (强平价 - 现价) / 现价
 *
 * 结果为负说明现价已经越过强平价（该成交却没成交，属于状态不一致），
 * 此时显式标注"已越过"而不是显示负百分比——后者看起来像 bug，
 * 前者是在如实报告一个需要人工介入的异常。
 */
const liqBuffer = computed(() => {
  const mark = props.position.mark_price
  const liq = props.position.liq_price
  if (!mark || !liq) return null
  const room = isShort.value ? (liq - mark) / mark : (mark - liq) / mark
  return {
    pct: room * 100,
    breached: room <= 0,
    // 阈值按"离爆仓还有多远"划：<15% 危险，<30% 需留意
    level: room <= 0 ? 'breach' : room < 0.15 ? 'danger' : room < 0.3 ? 'caution' : 'safe',
  }
})

// 文案带上方向，用户不需要自己判断"涨还是跌会爆仓"
const bufferLabel = computed(() => {
  if (!liqBuffer.value) return '距强平'
  if (liqBuffer.value.breached) return '已越过强平价'
  return isShort.value ? '再涨即强平' : '再跌即强平'
})

// 缓冲条：把剩余空间画成一条。
// clamp 下限 2% 保证"已经越界"时条仍可见（此时它表达的是"越界程度"），
// 上限 100% 防止高杠杆低波动时条撑满整个容器而失去刻度感。
const bufferWidth = computed(() => {
  if (!liqBuffer.value) return null
  return `${Math.min(100, Math.max(2, liqBuffer.value.pct * 3.2)).toFixed(1)}%`
})

function fmtNum(v, d = 4) {
  if (v == null || Number.isNaN(v)) return '-'
  if (Math.abs(v) >= 1000) return v.toFixed(1)
  if (Math.abs(v) >= 1) return v.toFixed(3)
  return Number(v).toPrecision(d)
}
function fmtUsd(v) {
  if (v == null) return '-'
  return v.toFixed(2)
}
function fmtTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ` +
         `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<template>
  <article class="pos" :class="isShort ? 'pos--short' : 'pos--long'">
    <header class="pos__head">
      <span class="tag" :class="isShort ? 'tag--down' : 'tag--up'">
        {{ isShort ? '空' : '多' }}
      </span>
      <b class="pos__inst">{{ (position.inst_id || '').split('-')[0] }}</b>
      <span class="pos__lev num">{{ position.leverage }}x</span>
      <span class="pos__pnl num" :class="pnlTone">
        {{ position.unrealised_pnl >= 0 ? '+' : '' }}{{ fmtUsd(position.unrealised_pnl) }}
        <small>USDT</small>
      </span>
    </header>

    <!-- 价格阶梯：从开仓 → 现价 → 强平，一行看清方向与终点 -->
    <div class="pos__ladder">
      <div class="ladder__step">
        <span class="ladder__k">开仓</span>
        <b class="ladder__v num">{{ fmtNum(position.entry_price) }}</b>
      </div>
      <span class="ladder__arrow" aria-hidden="true">→</span>
      <div class="ladder__step">
        <span class="ladder__k">标记价</span>
        <b class="ladder__v num">{{ fmtNum(position.mark_price) }}</b>
      </div>
      <span class="ladder__arrow" aria-hidden="true">→</span>
      <div class="ladder__step">
        <span class="ladder__k">强平价</span>
        <b class="ladder__v num tone-down">{{ fmtNum(position.liq_price) }}</b>
      </div>
    </div>

    <!-- 强平缓冲条 -->
    <div v-if="liqBuffer" class="buf" :class="'buf--' + liqBuffer.level">
      <div class="buf__meta">
        <span>{{ bufferLabel }}</span>
        <b class="num">{{ Math.abs(liqBuffer.pct).toFixed(1) }}%</b>
      </div>
      <div class="buf__track">
        <div class="buf__fill" :style="{ width: bufferWidth }"></div>
      </div>
    </div>
    <div v-else class="buf buf--unknown">
      <span class="buf__meta">距强平 —（缺少标记价）</span>
    </div>

    <footer class="pos__foot">
      <span class="num">名义 {{ fmtUsd(position.notional) }}</span>
      <span class="num">保证金 {{ fmtUsd(position.margin) }}</span>
      <span class="num tone-dim">{{ fmtTime(position.opened_at) }}</span>
      <button class="btn btn--danger pos__close"
              :disabled="busy" @click="emit('close', position)">
        {{ busy ? '平仓中…' : '市价平仓' }}
      </button>
    </footer>
  </article>
</template>

<style scoped>
.pos {
  padding: var(--sp-3);
  background: var(--bg-raised);
  border: 1px solid var(--line-soft);
  border-left: 3px solid var(--c-up);
  border-radius: var(--r-md);
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.pos--short { border-left-color: var(--c-down); }

.pos__head { display: flex; align-items: center; gap: var(--sp-2); }
.pos__inst { font-size: var(--fs-md); font-weight: var(--fw-bold); }
.pos__lev {
  font-size: var(--fs-2xs);
  color: var(--tx-tertiary);
  background: var(--bg-sunken);
  border-radius: var(--r-sm);
  padding: 1px 5px;
}
.pos__pnl {
  margin-left: auto;
  font-size: var(--fs-lg);
  font-weight: var(--fw-bold);
  line-height: 1;
}
.pos__pnl small {
  font-size: var(--fs-2xs);
  font-weight: var(--fw-normal);
  opacity: 0.65;
  margin-left: 2px;
}

/* 价格阶梯 */
.pos__ladder {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-2) var(--sp-3);
  background: var(--bg-sunken);
  border-radius: var(--r-sm);
}
.ladder__step { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.ladder__k { font-size: var(--fs-2xs); color: var(--tx-tertiary); }
.ladder__v { font-size: var(--fs-sm); font-weight: var(--fw-medium); }
.ladder__arrow { color: var(--tx-disabled); font-size: var(--fs-xs); }

/* 强平缓冲 */
.buf { display: flex; flex-direction: column; gap: var(--sp-1); }
.buf__meta {
  display: flex;
  justify-content: space-between;
  font-size: var(--fs-2xs);
  color: var(--tx-tertiary);
}
.buf__meta b { font-weight: var(--fw-bold); }

.buf__track {
  height: 4px;
  background: var(--bg-sunken);
  border-radius: var(--r-full);
  overflow: hidden;
}
.buf__fill {
  height: 100%;
  border-radius: var(--r-full);
  transition: width var(--dur) var(--ease), background var(--dur) var(--ease);
}
.buf--safe .buf__meta b { color: var(--tx-secondary); }
.buf--safe .buf__fill { background: var(--c-up); }
.buf--caution .buf__meta b { color: var(--c-warn); }
.buf--caution .buf__fill { background: var(--c-warn); }
.buf--danger .buf__meta b { color: var(--c-down); }
.buf--danger .buf__fill { background: var(--c-down); }
/* 越界：用条纹填充区分于"危险"。
   纯红色条与"缓冲不足"长得一样，用户会以为只是快爆仓了，
   而实际上它报告的是一个需要人工介入的状态不一致。 */
.buf--breach .buf__meta { color: var(--c-down); font-weight: var(--fw-medium); }
.buf--breach .buf__fill {
  background: repeating-linear-gradient(
    45deg,
    var(--c-down) 0 4px,
    transparent 4px 8px
  );
}
.buf--unknown .buf__meta { color: var(--tx-disabled); }

.pos__foot {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  font-size: var(--fs-2xs);
  color: var(--tx-tertiary);
  flex-wrap: wrap;
}
.pos__close { margin-left: auto; min-height: 28px; padding: 0 var(--sp-3); font-size: var(--fs-xs); }
</style>