import { apiFetch } from './client'
import type { Signal } from './types'

export const fetchSignals = () => apiFetch<Signal[]>('/api/signals/scan/all')
export interface LivePriceEntry { last_price: number; price_as_of: string }

export const fetchLivePrices = (symbols: string[]) =>
  apiFetch<Record<string, LivePriceEntry>>(
    `/api/signals/live-prices?symbols=${symbols.join(',')}`,
  )
