import { useEffect, useRef } from 'react'
import { useRealtimeStore } from '../store/realtime'

export function useNotifications() {
  const alerts = useRealtimeStore((s) => s.alerts)
  const lastSeenRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    if (!('Notification' in window)) return
    if (Notification.permission === 'default') {
      Notification.requestPermission()
    }
  }, [])

  useEffect(() => {
    if (Notification.permission !== 'granted') return
    if (document.visibilityState === 'visible') return

    for (const alert of alerts) {
      if (lastSeenRef.current.has(alert.id)) continue
      lastSeenRef.current.add(alert.id)

      const icons: Record<string, string> = {
        fill:   '✅',
        signal: '📡',
        stop:   '⚠️',
        error:  '❌',
      }

      try {
        new Notification(`${icons[alert.type] ?? '🔔'} Panel Trading`, {
          body: alert.message,
          tag: alert.id,
          silent: alert.type === 'signal',
        })
      } catch { /* ignore */ }
    }
  }, [alerts])
}

export function requestNotificationPermission() {
  if ('Notification' in window && Notification.permission === 'default') {
    return Notification.requestPermission()
  }
  return Promise.resolve(Notification.permission as NotificationPermission)
}
