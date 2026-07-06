import { useEffect, useRef } from 'react'
import { useAuthStore } from '../store/auth'
import { useRealtimeStore } from '../store/realtime'

const RECONNECT_DELAY_MS = 3000

export function useWebSocket() {
  const apiKey = useAuthStore((s) => s.apiKey)
  const { setConnected, updatePrice, addAlert } = useRealtimeStore()
  const wsRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!apiKey) return

    let cancelled = false

    function connect() {
      if (cancelled) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${proto}://${location.host}/ws?api_key=${apiKey}`
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => setConnected(true)

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === 'update') {
            // account/positions updates are handled by TanStack Query polling
          } else if (msg.type === 'price_update') {
            updatePrice(msg)
          } else if (msg.type === 'signal_alert' || msg.type === 'auto_trade_entry') {
            addAlert({ id: crypto.randomUUID(), type: 'signal', message: msg.message ?? msg.symbol, ts: Date.now() })
          } else if (msg.type === 'auto_trade_exit' || msg.type === 'auto_trade_scale_out') {
            addAlert({ id: crypto.randomUUID(), type: 'fill', message: `${msg.type}: ${msg.symbol}`, ts: Date.now() })
          } else if (msg.type === 'error') {
            addAlert({ id: crypto.randomUUID(), type: 'error', message: msg.message, ts: Date.now() })
          }
        } catch { /* ignore malformed */ }
      }

      ws.onclose = () => {
        setConnected(false)
        if (!cancelled) timerRef.current = setTimeout(connect, RECONNECT_DELAY_MS)
      }

      ws.onerror = () => ws.close()
    }

    connect()

    return () => {
      cancelled = true
      if (timerRef.current) clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [apiKey])
}
