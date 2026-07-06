import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchStatus, setMode, toggleHalt } from '../../api/account'
import { useRealtimeStore } from '../../store/realtime'
import { useAuthStore } from '../../store/auth'
import { StatusDot } from '../ui/StatusDot'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { fmtAge } from '../../lib/format'
import { useToastStore } from '../ui/Toast'

interface Props {
  onNewOrder?: () => void
  onConfig?: () => void
}

export function Header({ onNewOrder, onConfig }: Props) {
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: fetchStatus,
    refetchInterval: 5000,
  })

  const wsConnected = useRealtimeStore((s) => s.connected)
  const logout = useAuthStore((s) => s.logout)

  const haltMutation = useMutation({
    mutationFn: toggleHalt,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['status'] }),
    onError: () => addToast('Error al cambiar halt', 'error'),
  })

  const modeMutation = useMutation({
    mutationFn: () => setMode(status?.mode === 'live' ? 'paper' : 'live'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['status'] }),
    onError: () => addToast('Error al cambiar modo', 'error'),
  })

  return (
    <header className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800 shrink-0 gap-3">
      <div className="flex items-center gap-3 flex-wrap">
        {/* IBKR connection */}
        <div className="flex items-center gap-2">
          <StatusDot active={status?.connected ?? false} />
          <span className="text-xs text-gray-400">
            {status?.connected ? 'IBKR' : 'Desconectado'}
          </span>
        </div>

        {/* Mode toggle */}
        {status && (
          <button
            onClick={() => modeMutation.mutate()}
            disabled={modeMutation.isPending}
            title="Click para cambiar modo"
          >
            <Badge variant={status.mode === 'live' ? 'red' : 'yellow'}>
              {status.mode.toUpperCase()}
            </Badge>
          </button>
        )}

        {/* Halt toggle */}
        {status && (
          <button
            onClick={() => haltMutation.mutate()}
            disabled={haltMutation.isPending}
            title={status.halted ? 'Reanudar trading' : 'Pausar trading'}
          >
            <Badge variant={status.halted ? 'red' : 'gray'}>
              {status.halted ? 'HALTED' : 'ACTIVO'}
            </Badge>
          </button>
        )}

        {/* Scan age */}
        {status?.last_scan_at && (
          <span className="text-xs text-gray-500">
            Scan: {fmtAge(status.last_scan_at)}
            {status.scan_stale && <span className="text-yellow-500 ml-1">⚠</span>}
          </span>
        )}
      </div>

      <div className="flex items-center gap-2">
        {/* WebSocket */}
        <div className="flex items-center gap-1.5">
          <StatusDot active={wsConnected} pulse={wsConnected} />
          <span className="text-xs text-gray-500">WS</span>
        </div>

        {onNewOrder && (
          <Button variant="primary" size="sm" onClick={onNewOrder}>
            + Orden
          </Button>
        )}

        {onConfig && (
          <Button variant="ghost" size="sm" onClick={onConfig}>
            ⚙
          </Button>
        )}

        <button
          onClick={logout}
          className="text-xs text-gray-500 hover:text-gray-300 transition-colors px-1"
        >
          Salir
        </button>
      </div>
    </header>
  )
}
