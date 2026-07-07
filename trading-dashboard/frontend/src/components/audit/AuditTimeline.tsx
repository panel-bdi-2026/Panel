import { useQuery } from '@tanstack/react-query'
import { fetchAudit } from '../../api/audit'
import { fmtAge } from '../../lib/format'

const ACTION_LABELS: Record<string, string> = {
  auto_trade_entry:                'Entrada auto',
  auto_trade_exit:                 'Salida auto',
  auto_trade_protective_stop_placed: 'Stop protector',
  auto_trade_stop_updated:         'Stop actualizado',
  manual_order_created:            'Orden manual',
  order_approved:                  'Orden aprobada',
  order_rejected:                  'Orden rechazada',
  order_filled:                    'Orden ejecutada',
  fund_created:                    'Fondo creado',
  capital_flow:                    'Flujo capital',
  ibkr_connected:                  'IBKR conectado',
  ibkr_disconnected:               'IBKR desconectado',
  halt_changed:                    'Halt cambiado',
  mode_changed:                    'Modo cambiado',
  scan_complete:                   'Scan completado',
}

function actionColor(action: string) {
  if (action.includes('exit') || action.includes('stop')) return 'text-red-400'
  if (action.includes('entry') || action.includes('filled') || action.includes('approved')) return 'text-green-400'
  if (action.includes('rejected') || action.includes('disconnected')) return 'text-red-400'
  if (action.includes('created') || action.includes('connected')) return 'text-blue-400'
  return 'text-gray-400'
}

export function AuditTimeline() {
  const { data: entries, isLoading } = useQuery({
    queryKey: ['audit'],
    queryFn: () => fetchAudit(),
    refetchInterval: 15000,
  })

  if (isLoading) return (
    <div className="flex flex-col gap-1.5 animate-pulse">
      {[...Array(6)].map((_, i) => <div key={i} className="h-8 bg-gray-800 rounded-lg" />)}
    </div>
  )
  if (!entries?.length) return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <p className="text-2xl mb-2">📜</p>
      <p className="text-gray-400 font-medium">Sin entradas de auditoría</p>
    </div>
  )

  return (
    <div className="space-y-0.5 max-h-[70vh] overflow-y-auto scrollbar-thin">
      {entries.map((e) => {
        const symbol = e.payload?.symbol as string | undefined
        const fundId = e.payload?.fund_id as string | undefined
        return (
          <div key={e.id} className="flex items-start gap-3 px-3 py-2 hover:bg-gray-900 rounded-lg group">
            <span className="text-gray-600 text-xs w-12 shrink-0 pt-0.5 tabular-nums">
              {fmtAge(e.ts)}
            </span>
            <div className="flex-1 min-w-0">
              <span className={`text-xs font-medium ${actionColor(e.action)}`}>
                {ACTION_LABELS[e.action] ?? e.action.replace(/_/g, ' ')}
              </span>
              {symbol && (
                <span className="ml-2 text-xs text-gray-200 font-semibold">{symbol}</span>
              )}
              {e.result && Object.keys(e.result).length > 0 && (
                <p className="text-xs text-gray-600 mt-0.5 truncate">
                  {Object.entries(e.result)
                    .slice(0, 2)
                    .map(([k, v]) => `${k}: ${typeof v === 'number' ? (v as number).toFixed(2) : v}`)
                    .join(' · ')}
                </p>
              )}
            </div>
            {fundId && (
              <span className="text-gray-700 text-[10px] shrink-0 font-mono">
                {fundId.slice(0, 6)}
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
