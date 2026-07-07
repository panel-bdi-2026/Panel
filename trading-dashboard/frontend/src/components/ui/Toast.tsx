import { useEffect } from 'react'
import { create } from 'zustand'
import { useRealtimeStore } from '../../store/realtime'

// ── Manual toast store ────────────────────────────────────────────────────────

interface ToastItem {
  id: string
  message: string
  type: 'success' | 'error' | 'info' | 'warning'
}

interface ToastStore {
  toasts: ToastItem[]
  add: (message: string, type?: ToastItem['type']) => void
  remove: (id: string) => void
}

export const useToastStore = create<ToastStore>()((set) => ({
  toasts: [],
  add: (message, type = 'info') =>
    set((s) => ({
      toasts: [
        { id: crypto.randomUUID(), message, type },
        ...s.toasts,
      ].slice(0, 10),
    })),
  remove: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}))

// ── Toast items ───────────────────────────────────────────────────────────────

function ManualToastItem({ toast, onDismiss }: { toast: ToastItem; onDismiss: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 5000)
    return () => clearTimeout(t)
  }, [onDismiss])

  const colors: Record<ToastItem['type'], string> = {
    success: 'border-green-600 bg-green-950',
    error:   'border-red-600 bg-red-950',
    info:    'border-blue-600 bg-blue-950',
    warning: 'border-yellow-600 bg-yellow-950',
  }

  return (
    <div
      onClick={onDismiss}
      className={`flex items-start gap-3 px-4 py-3 rounded-lg border cursor-pointer shadow-lg text-sm ${colors[toast.type]}`}
    >
      <span className="flex-1 text-gray-200">{toast.message}</span>
      <span className="text-gray-500 text-xs shrink-0">&times;</span>
    </div>
  )
}

function WsAlertItem({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 5000)
    return () => clearTimeout(t)
  }, [onDismiss])

  return (
    <div
      onClick={onDismiss}
      className="flex items-start gap-3 px-4 py-3 rounded-lg border border-brand-600 bg-blue-950 cursor-pointer shadow-lg text-sm"
    >
      <span className="flex-1 text-gray-200">{message}</span>
      <span className="text-gray-500 text-xs shrink-0">&times;</span>
    </div>
  )
}

// ── Container ─────────────────────────────────────────────────────────────────

export function ToastContainer() {
  const { toasts, remove } = useToastStore()
  const { alerts, removeAlert } = useRealtimeStore()

  if (!toasts.length && !alerts.length) return null

  return (
    <div className="fixed bottom-4 right-4 z-[60] flex flex-col gap-2 w-80">
      {toasts.map((t) => (
        <ManualToastItem key={t.id} toast={t} onDismiss={() => remove(t.id)} />
      ))}
      {alerts.slice(0, 3).map((a) => (
        <WsAlertItem key={a.id} message={a.message} onDismiss={() => removeAlert(a.id)} />
      ))}
    </div>
  )
}
