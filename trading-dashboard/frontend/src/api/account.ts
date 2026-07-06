import { apiFetch } from './client'
import type { AccountSummary, AppStatus, Position } from './types'

export const fetchStatus = () => apiFetch<AppStatus>('/api/status')
export const fetchAccount = () => apiFetch<AccountSummary>('/api/account')
export const fetchPositions = () => apiFetch<Position[]>('/api/positions')

export const setMode = (mode: 'paper' | 'live') =>
  apiFetch<void>('/api/mode', { method: 'POST', body: JSON.stringify({ mode }) })

export const toggleHalt = () =>
  apiFetch<void>('/api/halt', { method: 'POST' })
