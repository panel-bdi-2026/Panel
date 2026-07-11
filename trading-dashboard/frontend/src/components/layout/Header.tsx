import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchStatus, setMode } from '../../api/account'
import { useRealtimeStore } from '../../store/realtime'
import { useAuthStore } from '../../store/auth'
import { Badge } from '../ui/Badge'
import { fmtAge } from '../../lib/format'
import { useToastStore } from '../ui/Toast'
import { Wifi, WifiOff, LogOut, Settings } from 'lucide-react'

interface Props {
  onNewOrder?: () => void
  onConfig?: () => void
}

export function Header({ onConfig }: Props) {
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: status } = useQuery({
    queryKey: ['status'],
    queryFn: fetchStatus,
    refetchInterval: 5000,
  })

  const wsConnected = useRealtimeStore((s) => s.connected)
  const logout = useAuthStore((s) => s.logout)

  const modeMutation = useMutation({
    mutationFn: () => setMode(status?.mode === 'live' ? 'paper' : 'live'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['status'] }),
    onError: () => addToast('Error al cambiar modo', 'error'),
  })

  const handleModeToggle = () => {
    if (status?.mode !== 'live') {
      if (!window.confirm('¿Activar modo LIVE? Las órdenes se ejecutarán con dinero real.')) return
    }
    modeMutation.mutate()
  }

  const ibkrConnected = status?.connected ?? false

  return (
    <header className="flex items-center justify-between px-3 sm:px-4 bg-gray-900 border-b border-gray-800 shrink-0 h-14 gap-2">
      {/* Left: status indicators */}
      <div className="flex items-center gap-2 sm:gap-3 min-w-0">
        {/* IBKR status */}
        <div
          className={`flex items-center gap-1.5 px-2 py-1 rounded-md text-xs font-medium ${
            ibkrConnected
              ? 'text-green-400 bg-green-400/10'
              : 'text-gray-500 bg-gray-800'
          }`}
          title={ibkrConnected ? 'IBKR conectado' : 'IBKR desconectado'}
        >
          {ibkrConnected
            ? <Wifi size={13} />
            : <WifiOff size={13} />}
          <span className="hidden sm:inline">{ibkrConnected ? 'IBKR' : 'Sin IBKR'}</span>
        </div>

        {/* Mode toggle */}
        {status && (
          <button
            onClick={handleModeToggle}
            disabled={modeMutation.isPending}
            title="Click para cambiar modo"
            className="cursor-pointer hover:opacity-80 transition-opacity"
          >
            <Badge variant={status.mode === 'live' ? 'red' : 'yellow'}>
              {status.mode.toUpperCase()}
            </Badge>
          </button>
        )}

        {/* Halt status — solo lectura (la acción de pausar/reanudar vive en la ControlBar) */}
        {status && (
          <span title={status.halted ? 'Trading pausado' : 'Trading activo'}>
            <Badge variant={status.halted ? 'red' : 'gray'}>
              {status.halted ? 'HALTED' : 'ACTIVO'}
            </Badge>
          </span>
        )}

        {/* Scan age — desktop only */}
        {status?.last_scan_at && (
          <span className="hidden md:flex items-center gap-1 text-xs text-gray-500">
            Scan: {fmtAge(status.last_scan_at)}
            {status.scan_stale && <span className="text-yellow-500">⚠</span>}
          </span>
        )}
      </div>

      {/* Right: actions */}
      <div className="flex items-center gap-1.5 sm:gap-2 shrink-0">
        {/* WebSocket indicator */}
        <div
          className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-green-400' : 'bg-gray-600'}`}
          title={wsConnected ? 'WebSocket conectado' : 'WebSocket desconectado'}
        />

        {onConfig && (
          <button
            onClick={onConfig}
            className="p-1.5 text-gray-500 hover:text-gray-200 hover:bg-gray-800 rounded-lg transition-colors"
            title="Configuración"
          >
            <Settings size={16} />
          </button>
        )}

        <button
          onClick={() => { if (window.confirm('¿Cerrar sesión?')) logout() }}
          className="p-1.5 text-gray-600 hover:text-gray-300 hover:bg-gray-800 rounded-lg transition-colors"
          title="Salir"
        >
          <LogOut size={15} />
        </button>
      </div>
    </header>
  )
}
