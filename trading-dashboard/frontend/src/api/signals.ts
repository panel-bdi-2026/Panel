import { apiFetch } from './client'
import type { Signal } from './types'

export const fetchSignals = () => apiFetch<Signal[]>('/api/signals/scan/all')
export const fetchLivePrices = () =>
  apiFetch<Record<string, { price: number; change_pct: number; ts: string }>>(
    '/api/signals/live-prices',
  )
