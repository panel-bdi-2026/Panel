import { useQuery } from '@tanstack/react-query'
import { fetchAudit } from '../../api/audit'
import { fmtAge } from '../../lib/format'

export function AuditTimeline() {
  const { data: entries, isLoading } = useQuery({
    queryKey: ['audit'],
    queryFn: () => fetchAudit(),
    refetchInterval: 15000,
  })

  if (isLoading) return <div className="text-gray-500 text-sm">Cargando auditoría…</div>
  if (!entries?.length) return <div className="text-gray-500 text-sm">Sin entradas de auditoría.</div>

  return (
    <div className="space-y-1 max-h-[70vh] overflow-y-auto">
      {entries.map((e) => (
        <div key={e.id} className="flex items-start gap-3 px-3 py-2 hover:bg-gray-900 rounded-lg">
          <span className="text-gray-600 text-xs w-28 shrink-0 pt-0.5">{fmtAge(e.ts)}</span>
          <div className="flex-1 min-w-0">
            <span className="text-gray-300 text-sm font-medium">{e.event}</span>
            {e.symbol && (
              <span className="ml-2 text-xs text-blue-400 font-semibold">{e.symbol}</span>
            )}
            {e.detail && (
              <p className="text-xs text-gray-500 mt-0.5 truncate">{e.detail}</p>
            )}
          </div>
          {e.fund_id && (
            <span className="text-gray-600 text-xs shrink-0">{e.fund_id.slice(0, 8)}</span>
          )}
        </div>
      ))}
    </div>
  )
}
