import { apiFetch } from './client'
import type { PendingOrder } from './types'

export const fetchPendingOrders = () => apiFetch<PendingOrder[]>('/api/orders/pending')

export const approveOrder = (id: string) =>
  apiFetch<PendingOrder>(`/api/orders/${id}/approve`, { method: 'POST' })

export const rejectOrder = (id: string, reason?: string) =>
  apiFetch<PendingOrder>(`/api/orders/${id}/reject`, {
    method: 'POST',
    body: JSON.stringify({ reason }),
  })

export const createOrder = (body: {
  fund_id: string
  symbol: string
  side: 'BUY' | 'SELL'
  quantity: number
  order_type?: string
  limit_price?: number | null
}) => apiFetch<PendingOrder>('/api/orders', { method: 'POST', body: JSON.stringify(body) })

export const fetchSizeSuggestion = (symbol: string, fund_id: string) =>
  apiFetch<{ quantity: number; stop_price: number | null }>(
    `/api/orders/size-suggestion?symbol=${symbol}&fund_id=${fund_id}`,
  )
