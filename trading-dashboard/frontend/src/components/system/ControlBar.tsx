import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchStatus, setHalt, reconnectIbkr } from '../../api/account'
import { fetchHealth } from '../../api/health'
import { useToastStore } from '../ui/Toast'
import { Play, Pause, Plug, Wifi, Plus, Loader2 } from 'lucide-react'

interface Props {
  onNewOrder: () => void
}

/**
 * Barra de controles críticos, siempre visible (también en móvil).
 * Patrón de "quick-action pills" del IBKR GlobalTrader, pero con nuestras
 * acciones de seguridad: pausar/reanudar, reconectar IBKR, nueva orden.
 */
export function ControlBar({ onNewOrder }: Props) {
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: fetchStatus,
    refetchInterval: 5000,
  })
  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 10000,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['status'] })
    qc.invalidateQueries({ queryKey: ['health'] })
  }

  const haltMutation = useMutation({
    mutationFn: (halted: boolean) => setHalt(halted),
    onSuccess: (_data, halted) => {
      invalidate()
      if (!halted && status && !status.connected) {
        addToast('Trading reanudado, pero IBKR sigue desconectado — reconectá para reanudar el scan.', 'warning')
      } else {
        addToast(halted ? 'Trading pausado' : 'Trading reanudado', 'success')
      }
    },
    onError: () => addToast('Error al cambiar el estado de trading', 'error'),
  })

  const reconnectMutation = useMutation({
    mutationFn: reconnectIbkr,
    onSuccess: () => { invalidate(); addToast('IBKR reconectado', 'success') },
    onError: (e: Error) => addToast(e.message || 'No se pudo reconectar IBKR', 'error'),
  })

  if (!status) return null

  const dotColor =
    !health || health.status === 'ok' ? 'bg-profit'
    : health.issues.includes('ibkr_disconnected') ? 'bg-loss'
    : 'bg-warn'

  const handleHalt = () => {
    const next = !status.halted
    if (!next) {
      const m = status.mode.toUpperCase()
      if (!window.confirm(`¿Reanudar trading en modo ${m}?`)) return
    }
    haltMutation.mutate(next)
  }

  const pill =
    'inline-flex items-center gap-1 h-7 px-2.5 rounded-full text-xs font-medium ' +
    'border transition-colors disabled:opacity-50 shrink-0 whitespace-nowrap'

  return (
    <div className="flex items-center gap-1.5 px-3 sm:px-4 py-1.5 bg-surface-1 border-b border-surface-3 overflow-x-auto scrollbar-none shrink-0">
      {/* Estado global */}
      <span className="flex items-center gap-1.5 shrink-0 mr-0.5">
        <span className={`w-2 h-2 rounded-full ${dotColor}`} />
      </span>

      {/* Pausar / Reanudar */}
      <button
        onClick={handleHalt}
        disabled={haltMutation.isPending}
        className={`${pill} ${
          status.halted
            ? 'border-profit/40 text-profit bg-profit/10 hover:bg-profit/20'
            : 'border-surface-3 text-gray-300 hover:bg-surface-2'
        }`}
      >
        {haltMutation.isPending ? <Loader2 size={13} className="animate-spin" />
          : status.halted ? <Play size={13} /> : <Pause size={13} />}
        {status.halted ? 'Reanudar' : 'Pausar'}
      </button>

      {/* Conectado: estado en verde (clickeable igual, por si se quiere forzar
          una reconexión). Desconectado: acción destacada en rojo. */}
      <button
        onClick={() => reconnectMutation.mutate()}
        disabled={reconnectMutation.isPending}
        title={status.connected ? 'Conectado — click para forzar reconexión' : 'Reconectar IBKR'}
        className={`${pill} ${
          !status.connected
            ? 'border-loss/50 text-loss bg-loss/10 hover:bg-loss/20'
            : 'border-profit/30 text-profit bg-profit/10 hover:bg-profit/20'
        }`}
      >
        {reconnectMutation.isPending
          ? <Loader2 size={13} className="animate-spin" />
          : status.connected ? <Wifi size={13} /> : <Plug size={13} />}
        {!status.connected ? 'Reconectar IBKR' : 'IBKR Conectado'}
      </button>

      {/* Nueva orden */}
      <button
        onClick={onNewOrder}
        className={`${pill} border-brand-600 text-white bg-brand-600 hover:bg-brand-500`}
      >
        <Plus size={13} /> Orden
      </button>
    </div>
  )
}
