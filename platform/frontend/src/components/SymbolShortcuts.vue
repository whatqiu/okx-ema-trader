<script setup>
import { computed, ref } from 'vue'
import { favorites, recentSymbols, normalizeSymbol, toggleFavorite, symbolStorageError } from '../symbolLists'
const props = defineProps({ symbol: { type: String, required: true } })
const emit = defineEmits(['select'])
const tab = ref('favorites')
const current = computed(() => normalizeSymbol(props.symbol))
const saved = computed(() => favorites.value.includes(current.value))
const list = computed(() => tab.value === 'favorites' ? favorites.value : recentSymbols.value)
</script>

<template>
  <nav class="shortcuts" aria-label="币种快捷切换">
    <div class="shortcuts__tabs" role="group" aria-label="币种列表">
      <button :class="{ active: tab === 'favorites' }" :aria-pressed="tab === 'favorites'" @click="tab = 'favorites'">我的收藏 <small>{{ favorites.length }}</small></button>
      <button :class="{ active: tab === 'recent' }" :aria-pressed="tab === 'recent'" @click="tab = 'recent'">最近查看</button>
    </div>
    <div class="shortcuts__list">
      <div v-for="s in list" :key="s" class="shortcut" :class="{ selected: current === s }">
        <button class="shortcut__select" :aria-current="current === s ? 'true' : undefined" :title="s" @click="emit('select', s)">{{ s.replace('-USDT-SWAP', '') }}<small>USDT</small></button>
        <button v-if="tab === 'favorites'" class="shortcut__remove" :aria-label="`取消收藏 ${s.replace('-USDT-SWAP', '')}`" @click="toggleFavorite(s)">×</button>
      </div>
      <span v-if="!list.length" class="shortcuts__empty">{{ tab === 'favorites' ? '收藏常看的币种，点击即可切换行情' : '查看过的币种会显示在这里' }}</span>
    </div>
    <button class="shortcuts__save" :class="{ saved }" :disabled="!current" :aria-pressed="saved" @click="toggleFavorite(current)">{{ saved ? '★ 已收藏' : '☆ 收藏当前' }}</button>
    <span v-if="symbolStorageError" class="shortcuts__empty" role="status">浏览器无法保存，收藏仅本次有效</span>
  </nav>
</template>

<style scoped>
.shortcuts { display: flex; align-items: center; gap: var(--sp-3); padding: var(--sp-2) var(--sp-4); background: var(--bg-base); border-bottom: 1px solid var(--line-soft); flex: 0 0 auto; min-width: 0; }
.shortcuts button { font: inherit; cursor: pointer; border: 0; background: transparent; color: var(--tx-secondary); min-height: 36px; border-radius: var(--r-sm); white-space: nowrap; }
.shortcuts button:hover { background: var(--bg-hover); color: var(--tx-primary); }
.shortcuts button:focus-visible { outline: 2px solid var(--c-accent); outline-offset: 2px; }
.shortcuts__tabs { display: flex; gap: var(--sp-1); flex-shrink: 0; }
.shortcuts__tabs button { padding: 0 var(--sp-2); font-size: var(--fs-sm); }
.shortcuts__tabs .active { color: var(--c-accent); background: var(--c-accent-soft); }
.shortcuts small { font-size: var(--fs-2xs); color: var(--tx-tertiary); margin-left: var(--sp-1); }
.shortcuts__list { display: flex; gap: var(--sp-2); align-items: center; flex: 1; min-width: 0; overflow-x: auto; padding: var(--sp-1); }
.shortcut { display: flex; align-items: center; border: 1px solid var(--line); border-radius: var(--r-md); flex-shrink: 0; }
.shortcut.selected { border-color: var(--c-accent); background: var(--c-accent-soft); }
.shortcut__select { padding: 0 var(--sp-3); font-weight: var(--fw-bold) !important; }
.shortcut.selected .shortcut__select { color: var(--c-accent); }
.shortcut__remove { padding: 0 var(--sp-2); }
.shortcuts__empty { font-size: var(--fs-xs); color: var(--tx-tertiary); white-space: nowrap; }
.shortcuts .shortcuts__save { padding: 0 var(--sp-3); border: 1px solid var(--line); font-size: var(--fs-sm); flex-shrink: 0; }
.shortcuts .saved { color: var(--c-accent); }
@media (max-width: 720px) {
  .shortcuts { flex-wrap: wrap; gap: var(--sp-1); }
  .shortcuts__list { order: 3; flex-basis: 100%; }
  .shortcuts__save { margin-left: auto; }
}
</style>
