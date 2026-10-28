<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ResizeGrip from './ResizeGrip.vue'

const props = defineProps({
  storageKey: { type: String, required: true },
  label: { type: String, required: true },
})
const panel = ref(null)
const measuredHeight = ref(240)
function readHeight() {
  try {
    const value = Number(localStorage.getItem(`panel-height:${props.storageKey}`))
    return Number.isFinite(value) && value >= 120 ? Math.min(1600, value) : null
  } catch { return null }
}
const height = ref(readHeight())
watch(() => props.storageKey, () => { height.value = readHeight() })
const style = computed(() => height.value == null ? {} : { height: `${height.value}px`, flex: '0 0 auto' })
function save(value) {
  height.value = value
  try { localStorage.setItem(`panel-height:${props.storageKey}`, String(value)) } catch { /* Storage may be unavailable. */ }
}
function reset() {
  height.value = null
  try { localStorage.removeItem(`panel-height:${props.storageKey}`) } catch { /* Keep resizing usable. */ }
}
function measure() {
  measuredHeight.value = panel.value?.getBoundingClientRect().height || 240
}
let observer
onMounted(() => {
  measure()
  observer = new ResizeObserver(measure)
  observer.observe(panel.value)
})
onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <section ref="panel" class="panel resizable-panel" :style="style">
    <div class="resizable-panel__content"><slot /></div>
    <ResizeGrip :model-value="height ?? measuredHeight" :measure="() => panel?.getBoundingClientRect().height" :label="`调整${label}高度`"
                @pointerenter="measure" @focusin="measure" @pointerdown.capture="measure"
                @update:model-value="save" @reset="reset" />
  </section>
</template>

<style scoped>
.resizable-panel { overflow: hidden; flex-shrink: 0; min-height: 120px; }
.resizable-panel__content { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: auto; }
.resizable-panel.panel--pad { padding-bottom: 0; }
.resizable-panel.panel--pad > .resize-grip { margin-top: 4px; }
</style>
