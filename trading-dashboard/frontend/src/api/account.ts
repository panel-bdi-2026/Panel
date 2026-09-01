import { apiFetch } from './client'
import type { AccountSummary, AppStatus, Position } from './types'

export const fetchStatus = () => apiFetch<AppStatus>('/api/status')
export const fetchAccount = () => apiFetch<AccountSummary>('/api/account')
export const fetchPositions = () => apiFetch<Position[]>('/api/positions')

export const setMode = (mode: 'paper' | 'live') =>
  apiFetch<void>('/api/mode', { method: 'POST', body: JSON.stringify({ mode }) })

// Pasa el valor explícito (?value=) — antes se llamaba sin value y el backend
// lo exigía, devolviendo 422: el botón de pausar/reanudar nunca funcionaba.
export const setHalt = (halted: boolean) =>
  apiFetch<{ halted: boolean }>(`/api/halt?value=${halted}`, { method: 'POST' })

// Reconecta a IBKR sin tocar modo ni halt. Recupera la conexión desde el UI
// cuando el Gateway se reinició y el backend quedó desconectado.
export const reconnectIbkr = () =>
  apiFetch<{ connected: boolean }>('/api/reconnect', { method: 'POST' })
