import { create } from 'zustand'

export interface WsPrice {
  symbol: string
  price: number
  change_pct: number
  ts: number
}

export interface WsAlert {
  id: string
  type: 'fill' | 'signal' | 'stop' | 'error'
  message: string
  ts: number
}

interface RealtimeState {
  connected: boolean
  prices: Record<string, WsPrice>
  alerts: WsAlert[]
  setConnected: (v: boolean) => void
  updatePrice: (p: WsPrice) => void
  addAlert: (a: WsAlert) => void
  removeAlert: (id: string) => void
  clearAlerts: () => void
}

export const useRealtimeStore = create<RealtimeState>()((set) => ({
  connected: false,
  prices: {},
  alerts: [],
  setConnected: (connected) => set({ connected }),
  updatePrice: (p) =>
    set((s) => ({ prices: { ...s.prices, [p.symbol]: p } })),
  addAlert: (a) =>
    set((s) => ({ alerts: [a, ...s.alerts].slice(0, 50) })),
  removeAlert: (id) =>
    set((s) => ({ alerts: s.alerts.filter((a) => a.id !== id) })),
  clearAlerts: () => set({ alerts: [] }),
}))
