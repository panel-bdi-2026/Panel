import { useEffect, useRef } from 'react'
import { useAuthStore } from '../store/auth'
import { useRealtimeStore } from '../store/realtime'

const RECONNECT_DELAY_BASE_MS = 1000
const RECONNECT_DELAY_MAX_MS = 30_000

export function useWebSocket() {
  const authed = useAuthStore((s) => s.authed)
  // Selectores individuales para evitar re-renders innecesarios cuando cambian
  // partes no relacionadas del store (M8).
  const setConnected = useRealtimeStore((s) => s.setConnected)
  const addAlert = useRealtimeStore((s) => s.addAlert)
  const wsRef = useRef<WebSocket | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const delayRef = useRef(RECONNECT_DELAY_BASE_MS)

  useEffect(() => {
    if (!authed) return

    let cancelled = false

    function connect() {
      if (cancelled) return
      const proto = location.protocol === 'https:' ? 'wss' : 'ws'
      // La cookie de sesión viaja automáticamente en el handshake (same-origin).
      // No se pasa ?api_key= para evitar que la clave quede expuesta en logs.
      const url = `${proto}://${location.host}/ws`
      const ws = new WebSocket(url)
      wsRef.current = ws

      ws.onopen = () => {
        setConnected(true)
        delayRef.current = RECONNECT_DELAY_BASE_MS
      }

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data)
          if (msg.type === 'signal_alert' || msg.type === 'auto_trade_entry') {
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
        if (!cancelled) {
          // Backoff exponencial: 1s → 2s → 4s → ... → 30s (M9)
          timerRef.current = setTimeout(connect, delayRef.current)
          delayRef.current = Math.min(delayRef.current * 2, RECONNECT_DELAY_MAX_MS)
        }
      }

      ws.onerror = () => ws.close()
    }

    connect()

    return () => {
      cancelled = true
      if (timerRef.current) clearTimeout(timerRef.current)
      wsRef.current?.close()
    }
  }, [authed])
}
