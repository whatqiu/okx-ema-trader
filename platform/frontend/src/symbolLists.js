import { ref } from 'vue'

export function normalizeSymbol(value) {
  const text = String(value || '').trim().toUpperCase()
  if (/^[A-Z0-9]+-USDT-SWAP$/.test(text)) return text
  if (/^[A-Z0-9]+$/.test(text)) {
    const base = text.endsWith('USDT') ? text.slice(0, -4) : text
    return base ? `${base}-USDT-SWAP` : ''
  }
  return ''
}
function read(key) {
  try {
    const list = JSON.parse(localStorage.getItem(key) || '[]')
    return Array.isArray(list) ? [...new Set(list.filter(s => typeof s === 'string').map(normalizeSymbol).filter(Boolean))] : []
  } catch { return [] }
}
export const favorites = ref(read('favoriteSymbols'))
export const recentSymbols = ref(read('recentSymbols').slice(0, 8))
export const symbolStorageError = ref(false)
function save(key, list) {
  try {
    localStorage.setItem(key, JSON.stringify(list))
    symbolStorageError.value = false
  } catch { symbolStorageError.value = true }
}
export function toggleFavorite(value) {
  const symbol = normalizeSymbol(value)
  if (!symbol) return
  favorites.value = favorites.value.includes(symbol)
    ? favorites.value.filter(s => s !== symbol) : [...favorites.value, symbol]
  save('favoriteSymbols', favorites.value)
}
export function rememberSymbol(value) {
  const symbol = normalizeSymbol(value)
  if (!symbol) return
  recentSymbols.value = [symbol, ...recentSymbols.value.filter(s => s !== symbol)].slice(0, 8)
  save('recentSymbols', recentSymbols.value)
}
