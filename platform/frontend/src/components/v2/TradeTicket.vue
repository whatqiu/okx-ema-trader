<script setup>
/**
 * 手动交易台。
 *
 * 设计要点：
 * 1) 下单按钮用方向色实心，且 hover 不变色 —— 避免"以为点的是空却是多"。
 * 2) 交易前把四个数说清楚：成交参考价、占用保证金、手续费、强平价。
 *    原版把它们塞在一行 11px 灰字里，实际等于隐藏。
 * 3) 仓位可用不足时按钮禁用并给出原因，而不是点了才报错。
 */
import { computed } from 'vue'

const props = defineProps({
  side: { type: String, required: true },
  refPrice: { type: Number, default: null },
  priceLabel: { type: String, default: '' },
  notional: { type: Number, required: true },
  leverage: { type: Number, required: true },
  available: { type: Number, default: 0 },
  busy: { type: Boolean, default: false },
})

const emit = defineEmits([
  'update:side', 'update:notional', 'update:leverage', 'submit', 'setPct',
])

const isLong = computed(() => props.side === 'long')

const margin = computed(() => {
  if (!props.leverage) return 0
  return props.notional / props.leverage
})

// 手续费：与 backend/paper.py 的 TAKER_FEE_BPS = 5.0 对齐（0.05%/边）。
// 开仓收一次、平仓再收一次，所以这里展示双边总额——
// 只报单边会让用户以为实际成本是显示值的一半。
const feeRate = 0.0005
const feeOpen = computed(() => props.notional * feeRate)
const feeRoundTrip = computed(() => feeOpen.value * 2)

/**
 * 预估强平价。
 *
 * 公式必须与 backend/paper.py 的 liquidation_price() 一致：
 *   entry * (1 - 1/lev)   多头
 *   entry * (1 + 1/lev)   空头
 *
 * 这里曾经多减了一个维持保证金系数（0.005），结果是交易台显示的强平价
 * 和成交后持仓卡显示的对不上——同一个数字在两个界面里不一样，
 * 用户会怀疑其中一个算错了。宁可两边都用后端的粗略口径，
 * 也不要各自"更精确"却互相矛盾。
 */
const liqEstimate = computed(() => {
  const p = props.refPrice
  if (!p || !props.leverage) return null
  return isLong.value ? p * (1 - 1 / props.leverage) : p * (1 + 1 / props.leverage)
})

// 所需保证金超过可用余额时不可下单。这里提前拦住，
// 而不是让后端返回 400 后用户在横幅里读错误信息。
const insufficient = computed(() => margin.value > props.available && props.available > 0)
const canSubmit = computed(() =>
  !props.busy && props.notional > 0 && props.leverage > 0 && !insufficient.value)

const blockedReason = computed(() => {
  if (props.busy) return '下单中…'
  if (insufficient.value) {
    return `保证金 ${margin.value.toFixed(2)} 超出可用 ${props.available.toFixed(2)}`
  }
  return isLong.value ? '买入开多' : '卖出开空'
})

function fmtNum(v, d = 4) {
  if (v == null || Number.isNaN(v)) return '-'
  if (Math.abs(v) >= 1000) return v.toFixed(1)
  if (Math.abs(v) >= 1) return v.toFixed(3)
  return Number(v).toPrecision(d)
}
</script>

<template>
  <div class="ticket">
    <!-- 方向选择 -->
    <div class="segment" role="group" aria-label="交易方向">
      <button class="segment__btn" :class="{ 'is-long': isLong }"
              :aria-pressed="isLong"
              @click="emit('update:side', 'long')">开多</button>
      <button class="segment__btn" :class="{ 'is-short': !isLong }"
              :aria-pressed="!isLong"
              @click="emit('update:side', 'short')">开空</button>
    </div>

    <!-- 成交参考价：单独成块，因为它是"实际会成交在哪个价"的唯一提示 -->
    <div class="fill-hint" :class="isLong ? 'tone-up' : 'tone-down'">
      <div class="fill-hint__row">
        <span class="fill-hint__k">市价成交参考</span>
        <span class="tag" :class="isLong ? 'tag--up' : 'tag--down'">
          {{ priceLabel || (isLong ? '吃卖一' : '吃买一') }}
        </span>
      </div>
      <div class="fill-hint__price num">{{ fmtNum(refPrice) }}</div>
    </div>

    <!-- 名义价值 -->
    <div class="field">
      <label class="field__label" for="tk-notional">
        <span>名义价值</span>
        <span class="tone-accent">USDT</span>
      </label>
      <input id="tk-notional"
             class="input input--num"
             type="number" min="1" step="10"
             :value="notional"
             @input="emit('update:notional', Number($event.target.value))" />
    </div>

    <!-- 快捷金额 -->
    <div class="quick">
      <button v-for="n in [100, 500, 1000, 5000]" :key="n" class="chip"
              :class="{ 'is-on': notional === n }"
              @click="emit('update:notional', n)">{{ n }}</button>
    </div>

    <!-- 按可用余额百分比 -->
    <div class="quick">
      <button v-for="p in [10, 25, 50, 100]" :key="p" class="chip"
              @click="emit('setPct', p / 100)">{{ p }}%</button>
      <span class="quick__note num">可用 {{ available.toFixed(2) }}</span>
    </div>

    <!-- 杠杆 -->
    <div class="field">
      <label class="field__label" for="tk-lev">
        <span>杠杆</span>
        <b class="tone-accent num">{{ leverage }}x</b>
      </label>
      <input id="tk-lev"
             class="slider"
             type="range" min="1" max="20" step="1"
             :value="leverage"
             @input="emit('update:leverage', Number($event.target.value))" />
      <div class="lev-scale">
        <span>1x</span><span>5x</span><span>10x</span><span>20x</span>
      </div>
    </div>

    <!-- 成本明细：下单前必须让用户看到这四个数 -->
    <dl class="breakdown">
      <div class="breakdown__row">
        <dt>占用保证金</dt>
        <dd class="num">{{ margin.toFixed(2) }}</dd>
      </div>
      <div class="breakdown__row">
        <dt>手续费 0.05%<span class="tone-dim"> 开+平</span></dt>
        <dd class="num">{{ feeRoundTrip.toFixed(2) }}</dd>
      </div>
      <div class="breakdown__row">
        <dt>预估强平价 <span class="tone-dim">估算</span></dt>
        <dd class="num tone-down">{{ fmtNum(liqEstimate) }}</dd>
      </div>
    </dl>

    <button class="btn btn--lg btn--block submit"
            :class="isLong ? 'submit--long' : 'submit--short'"
            :disabled="!canSubmit"
            @click="emit('submit')">
      {{ blockedReason }}
    </button>
  </div>
</template>

<style scoped>
.ticket { display: flex; flex-direction: column; gap: var(--sp-3); }

.fill-hint {
  padding: var(--sp-3);
  background: var(--bg-sunken);
  border: 1px solid var(--line-soft);
  border-radius: var(--r-md);
}
.fill-hint__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}
.fill-hint__k { font-size: var(--fs-xs); color: var(--tx-tertiary); }
.fill-hint__price {
  font-size: var(--fs-xl);
  font-weight: var(--fw-bold);
  line-height: 1.1;
  margin-top: var(--sp-1);
}

.quick { display: flex; align-items: center; gap: var(--sp-2); flex-wrap: wrap; }
.quick__note {
  margin-left: auto;
  font-size: var(--fs-2xs);
  color: var(--tx-tertiary);
}

.lev-scale {
  display: flex;
  justify-content: space-between;
  font-size: var(--fs-2xs);
  color: var(--tx-disabled);
  margin-top: -4px;
}

.breakdown {
  margin: 0;
  padding: var(--sp-3);
  background: var(--bg-sunken);
  border-radius: var(--r-md);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.breakdown__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
}
.breakdown dt { font-size: var(--fs-xs); color: var(--tx-tertiary); }
.breakdown dd {
  margin: 0;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
}

/* 下单按钮：颜色即方向，不做 hover 变色——在最后一刻改变按钮外观
   会让人怀疑自己点错了。hover 只给阴影反馈。 */
.submit { margin-top: var(--sp-1); }
.submit--long {
  background: var(--c-up);
  color: var(--tx-inverse);
  border-color: transparent;
}
.submit--long:hover:not(:disabled) {
  background: var(--c-up);
  border-color: transparent;
  box-shadow: 0 4px 14px var(--c-up-soft);
}
.submit--short {
  background: var(--c-down);
  color: #fff;
  border-color: transparent;
}
.submit--short:hover:not(:disabled) {
  background: var(--c-down);
  border-color: transparent;
  box-shadow: 0 4px 14px var(--c-down-soft);
}
</style>