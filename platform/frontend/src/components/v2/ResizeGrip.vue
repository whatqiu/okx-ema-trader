<script setup>
import { onBeforeUnmount, ref } from 'vue'

const props = defineProps({
  modelValue: { type: Number, required: true },
  min: { type: Number, default: 120 },
  max: { type: Number, default: 1600 },
  axis: { type: String, default: 'y' },
  direction: { type: Number, default: 1 },
  label: { type: String, required: true },
  measure: { type: Function, default: null },
})
const emit = defineEmits(['update:modelValue', 'reset'])
const active = ref(false)
let drag = null

function update(value) {
  emit('update:modelValue', Math.round(Math.max(props.min, Math.min(props.max, value))))
}
function start(event) {
  if (event.button !== 0) return
  event.preventDefault()
  event.currentTarget.setPointerCapture(event.pointerId)
  drag = { id: event.pointerId, at: props.axis === 'x' ? event.clientX : event.clientY, size: props.measure?.() ?? props.modelValue }
  active.value = true
}
function move(event) {
  if (!drag || event.pointerId !== drag.id) return
  const at = props.axis === 'x' ? event.clientX : event.clientY
  update(drag.size + (at - drag.at) * props.direction)
}
function stop() {
  drag = null
  active.value = false
}
function keydown(event) {
  const negative = props.axis === 'x' ? 'ArrowLeft' : 'ArrowUp'
  const positive = props.axis === 'x' ? 'ArrowRight' : 'ArrowDown'
  if (event.key === negative || event.key === positive) {
    event.preventDefault()
    update((props.measure?.() ?? props.modelValue) + (event.key === positive ? 1 : -1) * props.direction * (event.shiftKey ? 40 : 10))
  } else if (event.key === 'Home') {
    event.preventDefault()
    emit('reset')
  }
}
onBeforeUnmount(stop)
</script>

<template>
  <div class="resize-grip" :class="[`resize-grip--${axis}`, { 'is-active': active }]"
       role="separator" tabindex="0" :aria-label="label"
       :aria-orientation="axis === 'x' ? 'vertical' : 'horizontal'"
       :aria-valuenow="Math.round(modelValue)" :aria-valuemin="min" :aria-valuemax="max"
       :title="`${label}：拖动或方向键调整，双击恢复默认`"
       @pointerdown="start" @pointermove="move" @pointerup="stop"
       @pointercancel="stop" @lostpointercapture="stop" @keydown="keydown" @dblclick="emit('reset')">
    <span></span>
  </div>
</template>

<style scoped>
.resize-grip { display: flex; align-items: center; justify-content: center; touch-action: none; user-select: none; flex-shrink: 0; z-index: 2; }
.resize-grip--x { width: 8px; cursor: col-resize; }
.resize-grip--y { height: 12px; width: 100%; cursor: row-resize; }
.resize-grip span { border-radius: 4px; background: var(--line-strong); }
.resize-grip--x span { width: 3px; height: 32px; }
.resize-grip--y span { width: 32px; height: 3px; }
.resize-grip:hover span, .resize-grip:focus-visible span, .resize-grip.is-active span { background: var(--c-accent); }
.resize-grip:focus-visible { outline: 1px solid var(--c-accent); outline-offset: -1px; }
</style>
