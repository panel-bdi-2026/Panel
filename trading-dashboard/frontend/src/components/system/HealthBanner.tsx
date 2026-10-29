import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchHealth } from '../../api/health'
import { setHalt, reconnectIbkr } from '../../api/account'
import { useToastStore } from '../ui/Toast'
import { fmtAge } from '../../lib/format'
import { AlertTriangle, Plug, Play, Loader2 } from 'lucide-react'

/**
 * Banner accionable que aparece solo cuando el sistema está degradado.
 * Muestra los issues activos con un botón de acción por cada uno.
 */
export function HealthBanner() {
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: fetchHealth,
    refetchInterval: 10000,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['status'] })
    qc.invalidateQueries({ queryKey: ['health'] })
  }

  const reconnectMutation = useMutation({
    mutationFn: reconnectIbkr,
    onSuccess: () => { invalidate(); addToast('IBKR reconectado', 'success') },
    onError: (e: Error) => addToast(e.message || 'No se pudo reconectar', 'error'),
  })
  const resumeMutation = useMutation({
    mutationFn: () => setHalt(false),
    onSuccess: () => { invalidate(); addToast('Trading reanudado', 'success') },
    onError: () => addToast('Error al reanudar', 'error'),
  })

  if (!health || health.status === 'ok' || !health.issues.length) return null

  const critical = health.issues.includes('ibkr_disconnected')

  return (
    <div className={`flex flex-wrap items-center gap-x-4 gap-y-2 px-3 sm:px-4 py-2.5 border-b text-sm shrink-0 ${
      critical
        ? 'bg-loss/10 border-loss/30 text-loss'
        : 'bg-warn/10 border-warn/30 text-warn'
    }`}>
      <span className="flex items-center gap-2 font-medium">
        <AlertTriangle size={16} className="shrink-0" />
        Sistema degradado
      </span>

      {health.issues.includes('ibkr_disconnected') && (
        <button
          onClick={() => reconnectMutation.mutate()}
          disabled={reconnectMutation.isPending}
          className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md bg-loss/20 hover:bg-loss/30 font-medium disabled:opacity-50"
        >
          {reconnectMutation.isPending ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />}
          IBKR desconectado — Reconectar
        </button>
      )}

      {health.issues.includes('trading_halted') && (
        <button
          onClick={() => resumeMutation.mutate()}
          disabled={resumeMutation.isPending}
          className="inline-flex items-center gap-1.5 px-3 py-1 rounded-md bg-warn/20 hover:bg-warn/30 font-medium disabled:opacity-50"
        >
          {resumeMutation.isPending ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
          Trading pausado — Reanudar
        </button>
      )}

      {health.issues.includes('scan_stale') && (
        <span className="text-warn/90">
          Scan detenido{health.last_scan_at ? ` — último hace ${fmtAge(health.last_scan_at)}` : ''}
        </span>
      )}

      {health.issues.includes('market_data_degraded') && (
        <span className="text-warn/90">Datos de mercado degradados</span>
      )}
    </div>
  )
}
