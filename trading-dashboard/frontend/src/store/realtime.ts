import { create } from 'zustand'

export interface WsAlert {
  id: string
  type: 'fill' | 'signal' | 'stop' | 'error'
  message: string
  ts: number
}

interface RealtimeState {
  connected: boolean
  alerts: WsAlert[]
  setConnected: (v: boolean) => void
  addAlert: (a: WsAlert) => void
  removeAlert: (id: string) => void
  clearAlerts: () => void
}

export const useRealtimeStore = create<RealtimeState>()((set) => ({
  connected: false,
  alerts: [],
  setConnected: (connected) => set({ connected }),
  addAlert: (a) =>
    set((s) => ({ alerts: [a, ...s.alerts].slice(0, 50) })),
  removeAlert: (id) =>
    set((s) => ({ alerts: s.alerts.filter((a) => a.id !== id) })),
  clearAlerts: () => set({ alerts: [] }),
}))
