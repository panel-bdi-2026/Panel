import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchStatus, setHalt, setMode, reconnectIbkr } from '../../api/account'
import { fetchHealth } from '../../api/health'
import { useRealtimeStore } from '../../store/realtime'
import { useAuthStore } from '../../store/auth'
import { Badge } from '../ui/Badge'
import { fmtAge } from '../../lib/format'
import { useToastStore } from '../ui/Toast'
import { Play, Pause, Plug, Wifi, Plus, Loader2, Settings, LogOut } from 'lucide-react'

interface Props {
  onNewOrder: () => void
  onConfig?: () => void
}

/**
 * Barra de controles críticos, siempre visible (también en móvil) — una
 * sola fila con el modo (PAPER/LIVE), pausar/reanudar, IBKR y nueva orden a
 * la izquierda, y config/logout a la derecha. Antes eran dos filas
 * separadas (Header + ControlBar) que mostraban info parcialmente
 * duplicada (IBKR/HALTED en el Header solo aparecían en pantallas grandes
 * como refuerzo visual de lo que esta fila ya muestra en pills) — unificarlas
 * ahorra una fila entera de alto en móvil sin perder información real.
 */
export function ControlBar({ onNewOrder, onConfig }: Props) {
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)
  const wsConnected = useRealtimeStore((s) => s.connected)
  const logout = useAuthStore((s) => s.logout)

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

  const modeMutation = useMutation({
    mutationFn: () => setMode(status?.mode === 'live' ? 'paper' : 'live'),
    onSuccess: () => invalidate(),
    onError: () => addToast('Error al cambiar modo', 'error'),
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

  const handleModeToggle = () => {
    if (status.mode !== 'live') {
      if (!window.confirm('¿Activar modo LIVE? Las órdenes se ejecutarán con dinero real.')) return
    }
    modeMutation.mutate()
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

      {/* Modo PAPER/LIVE */}
      <button
        onClick={handleModeToggle}
        disabled={modeMutation.isPending}
        title="Click para cambiar modo"
        className="shrink-0 hover:opacity-80 transition-opacity"
      >
        <Badge variant={status.mode === 'live' ? 'red' : 'yellow'}>
          {status.mode.toUpperCase()}
        </Badge>
      </button>

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

      {status.last_scan_at && (
        <span className="hidden md:flex items-center gap-1 text-xs text-gray-500 shrink-0">
          Scan: {fmtAge(status.last_scan_at)}
          {status.scan_stale && <span className="text-yellow-500">⚠</span>}
        </span>
      )}

      {/* Derecha: WS, config, logout */}
      <div className="flex items-center gap-1.5 shrink-0 ml-auto pl-1.5">
        <span
          className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-green-400' : 'bg-gray-600'}`}
          title={wsConnected ? 'WebSocket conectado' : 'WebSocket desconectado'}
        />

        {onConfig && (
          <button
            onClick={onConfig}
            className="p-1.5 text-gray-500 hover:text-gray-200 hover:bg-surface-2 rounded-lg transition-colors"
            title="Configuración"
          >
            <Settings size={16} />
          </button>
        )}

        <button
          onClick={() => { if (window.confirm('¿Cerrar sesión?')) logout() }}
          className="p-1.5 text-gray-600 hover:text-gray-300 hover:bg-surface-2 rounded-lg transition-colors"
          title="Salir"
        >
          <LogOut size={15} />
        </button>
      </div>
    </div>
  )
}
