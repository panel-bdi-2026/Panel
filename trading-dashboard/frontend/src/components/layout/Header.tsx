import { useQuery } from '@tanstack/react-query'
import { fetchStatus } from '../../api/account'
import { useRealtimeStore } from '../../store/realtime'
import { useAuthStore } from '../../store/auth'
import { StatusDot } from '../ui/StatusDot'
import { Badge } from '../ui/Badge'
import { fmtAge } from '../../lib/format'

export function Header() {
  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: fetchStatus,
    refetchInterval: 5000,
  })

  const wsConnected = useRealtimeStore((s) => s.connected)
  const logout = useAuthStore((s) => s.logout)

  return (
    <header className="flex items-center justify-between px-4 py-2.5 bg-gray-900 border-b border-gray-800 shrink-0">
      <div className="flex items-center gap-4">
        {/* IBKR connection */}
        <div className="flex items-center gap-2">
          <StatusDot active={status?.connected ?? false} />
          <span className="text-xs text-gray-400">
            {status?.connected ? 'IBKR' : 'Desconectado'}
          </span>
        </div>

        {/* Mode */}
        {status && (
          <Badge variant={status.mode === 'live' ? 'red' : 'yellow'}>
            {status.mode.toUpperCase()}
          </Badge>
        )}

        {/* Halted */}
        {status?.halted && (
          <Badge variant="red">HALT</Badge>
        )}

        {/* Scan age */}
        {status?.last_scan_at && (
          <span className="text-xs text-gray-500">
            Scan: {fmtAge(status.last_scan_at)}
            {status.scan_stale && <span className="text-yellow-500 ml-1">⚠</span>}
          </span>
        )}
      </div>

      <div className="flex items-center gap-3">
        {/* WebSocket */}
        <div className="flex items-center gap-1.5">
          <StatusDot active={wsConnected} pulse={wsConnected} />
          <span className="text-xs text-gray-500">WS</span>
        </div>

        <button
          onClick={logout}
          className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
        >
          Salir
        </button>
      </div>
    </header>
  )
}
