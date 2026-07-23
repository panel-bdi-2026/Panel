import { apiFetch } from './client'
import type { Signal } from './types'

export const fetchSignals = () =>
  apiFetch<{ as_of: string; results: Signal[] }>('/api/signals/scan/all')
    .then((r) => r.results)
export interface LivePriceEntry { last_price: number; price_as_of: string }

export const fetchLivePrices = (symbols: string[]) =>
  apiFetch<Record<string, LivePriceEntry>>(
    `/api/signals/live-prices?symbols=${symbols.join(',')}`,
  )

export const fetchCompanyNames = (symbols: string[]) =>
  apiFetch<Record<string, string>>(
    `/api/company-names?symbols=${symbols.join(',')}`,
  )
