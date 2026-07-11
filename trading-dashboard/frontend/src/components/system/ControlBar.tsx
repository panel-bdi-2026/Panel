import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchStatus, setHalt, reconnectIbkr } from '../../api/account'
import { fetchHealth } from '../../api/health'
import { useToastStore } from '../ui/Toast'
import { Play, Pause, Plug, Plus, Loader2 } from 'lucide-react'

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
    'inline-flex items-center gap-1.5 h-11 sm:h-9 px-3 rounded-full text-sm font-medium ' +
    'border transition-colors disabled:opacity-50 shrink-0 whitespace-nowrap'

  return (
    <div className="flex items-center gap-2 px-3 sm:px-4 py-2 bg-surface-1 border-b border-surface-3 overflow-x-auto scrollbar-none shrink-0">
      {/* Estado global */}
      <span className="flex items-center gap-1.5 shrink-0 mr-1">
        <span className={`w-2.5 h-2.5 rounded-full ${dotColor}`} />
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
        {haltMutation.isPending ? <Loader2 size={15} className="animate-spin" />
          : status.halted ? <Play size={15} /> : <Pause size={15} />}
        {status.halted ? 'Reanudar' : 'Pausar'}
      </button>

      {/* Reconectar IBKR — destacado si está desconectado */}
      <button
        onClick={() => reconnectMutation.mutate()}
        disabled={reconnectMutation.isPending}
        className={`${pill} ${
          !status.connected
            ? 'border-loss/50 text-loss bg-loss/10 hover:bg-loss/20'
            : 'border-surface-3 text-gray-400 hover:bg-surface-2'
        }`}
      >
        {reconnectMutation.isPending
          ? <Loader2 size={15} className="animate-spin" />
          : <Plug size={15} />}
        {!status.connected ? 'Reconectar IBKR' : 'Reconectar'}
      </button>

      {/* Nueva orden */}
      <button
        onClick={onNewOrder}
        className={`${pill} border-brand-600 text-white bg-brand-600 hover:bg-brand-500`}
      >
        <Plus size={15} /> Orden
      </button>
    </div>
  )
}
