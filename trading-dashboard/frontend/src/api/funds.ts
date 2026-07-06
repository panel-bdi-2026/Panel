import { apiFetch } from './client'
import type { Fund } from './types'

export const fetchFunds = () => apiFetch<Fund[]>('/api/funds')

export const fetchFund = (id: string) => apiFetch<Fund>(`/api/funds/${id}`)

export const createFund = (body: {
  name: string
  initial_capital_usd: number
  auto_trading_enabled?: boolean
  strategy_id?: string | null
}) => apiFetch<Fund>('/api/funds', { method: 'POST', body: JSON.stringify(body) })

export const closeFund = (id: string) =>
  apiFetch<Fund>(`/api/funds/${id}/close`, { method: 'POST' })

export const addCapitalFlow = (id: string, amount: number, note?: string) =>
  apiFetch<Fund>(`/api/funds/${id}/capital-flows`, {
    method: 'POST',
    body: JSON.stringify({ amount, note }),
  })

export const setAutoTrading = (id: string, enabled: boolean) =>
  apiFetch<Fund>(`/api/funds/${id}/auto-trading`, {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  })

export interface RoiPoint { date: string; roi_pct: number }
export const fetchRoiHistory = () =>
  apiFetch<Record<string, RoiPoint[]>>('/api/funds/roi-history')
