// One fetch wrapper for the whole UI: every failure becomes a Chinese sentence
// the user can act on, never a bare "Failed to fetch".
const BASE = ''

async function request(path, options = {}) {
  let res
  try {
    res = await fetch(BASE + path, options)
  } catch (err) {
    throw new Error('连不上后端服务（uvicorn 是否在 8788 端口运行？）')
  }
  let body = null
  try {
    body = await res.json()
  } catch {
    throw new Error(`后端返回了非 JSON 内容（HTTP ${res.status}）`)
  }
  if (!res.ok) {
    throw new Error(body?.detail || `HTTP ${res.status}`)
  }
  return body
}

export const api = {
  // Liveness of this process only (never upstream state) — see backend note.
  health: () => request('/api/health'),
  // Upstream connectivity + the circuit breaker: "can we still see the market".
  status: () => request('/api/status'),
  riskCheck: () => request('/api/risk/check', { method: 'POST' }),
  riskReset: () => request('/api/risk/reset', { method: 'POST' }),
  candles: (symbol, bar, limit = 500, sync = true, since = 0) =>
    request(`/api/candles?symbol=${encodeURIComponent(symbol)}&bar=${bar}&limit=${limit}&sync=${sync}${since > 0 ? `&since=${since}` : ''}`),
  ticker: (symbol) => request(`/api/ticker?symbol=${encodeURIComponent(symbol)}`),
  orders: ({ symbol, status, limit = 100 } = {}) => {
    const p = new URLSearchParams()
    if (symbol) p.set('symbol', symbol)
    if (status) p.set('status', status)
    p.set('limit', limit)
    return request(`/api/orders?${p}`)
  },
  instruments: (q = '', limit = 30) =>
    request(`/api/instruments?q=${encodeURIComponent(q)}&limit=${limit}`),
  signals: (symbol, limit = 100) =>
    request(`/api/signals?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`),
  backtests: (symbol, limit = 30) =>
    request(`/api/backtests?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`),
  runBacktest: (payload) =>
    request('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  stats: () => request('/api/stats'),
  equity: (limit = 500) => request(`/api/equity?limit=${limit}`),
  account: () => request('/api/account'),
  openTrade: (payload) =>
    request('/api/trade/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  closeTrade: (orderId) =>
    request('/api/trade/close', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ order_id: orderId }),
    }),
  resetAccount: (balance = 10000) =>
    request('/api/account/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ balance }),
    }),
  settings: () => request('/api/settings'),
  saveSettings: (patch) =>
    request('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  autotrade: () => request('/api/autotrade'),
  setAutotrade: (patch) =>
    request('/api/autotrade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
}
