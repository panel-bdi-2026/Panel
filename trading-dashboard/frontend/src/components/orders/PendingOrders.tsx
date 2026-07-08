import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchPendingOrders, approveOrder, rejectOrder } from '../../api/orders'
import { fmtUsd, fmtAge } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { useToastStore } from '../ui/Toast'

export function PendingOrders() {
  const { data: orders, isLoading } = useQuery({
    queryKey: ['orders', 'pending'],
    queryFn: fetchPendingOrders,
    refetchInterval: 5000,
  })
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const approve = useMutation({
    mutationFn: approveOrder,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['orders'] }); addToast('Orden aprobada', 'success') },
    onError: () => addToast('Error al aprobar orden', 'error'),
  })
  const reject = useMutation({
    mutationFn: (id: string) => rejectOrder(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['orders'] }); addToast('Orden rechazada', 'info') },
    onError: () => addToast('Error al rechazar orden', 'error'),
  })

  if (isLoading) return (
    <div className="space-y-2 animate-pulse">
      {[...Array(3)].map((_, i) => <div key={i} className="h-16 bg-gray-800 rounded-xl" />)}
    </div>
  )
  if (!orders?.length) return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <p className="text-2xl mb-2">📋</p>
      <p className="text-gray-400 font-medium">Sin órdenes pendientes</p>
    </div>
  )

  const pending = orders.filter((o) => o.status === 'pending')
  const rest = orders.filter((o) => o.status !== 'pending')

  const statusLabel: Record<string, string> = {
    filled: 'Ejecutada',
    rejected: 'Rechazada',
    cancelled: 'Cancelada',
    expired: 'Expirada',
  }

  return (
    <div className="space-y-6">
      {pending.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">
            Pendientes de aprobación ({pending.length})
          </h3>
          <div className="space-y-2">
            {pending.map((o) => (
              <div key={o.id} className="bg-gray-900 border border-yellow-700/40 rounded-xl px-4 py-3">
                {/* Main row */}
                <div className="flex items-center gap-3">
                  <Badge variant={o.order.side === 'BUY' ? 'green' : 'red'}>{o.order.side}</Badge>
                  <span className="font-bold text-gray-100 text-base">{o.order.symbol}</span>
                  <span className="text-gray-300 text-sm">{o.order.quantity} acc.</span>
                  <span className="text-gray-500 text-xs">{o.order.order_type}</span>
                  {o.order.limit_price && (
                    <span className="text-gray-400 text-xs">@ {fmtUsd(o.order.limit_price)}</span>
                  )}
                  <span className="text-gray-600 text-xs ml-auto shrink-0">{fmtAge(o.created_at)}</span>
                </div>
                {/* Estimated value + violations */}
                {o.decision && (
                  <div className="flex flex-wrap items-start gap-x-3 gap-y-1 mt-1">
                    {o.decision.estimated_value_usd && (
                      <span className="text-xs text-gray-500 shrink-0">{fmtUsd(o.decision.estimated_value_usd)}</span>
                    )}
                    {o.decision.violations.map((v, i) => (
                      <span key={i} className="text-xs text-red-400 break-words">{v}</span>
                    ))}
                  </div>
                )}
                {o.notes && (
                  <p className="text-gray-500 text-xs mt-1 line-clamp-2">{o.notes}</p>
                )}
                {/* Actions */}
                <div className="flex gap-2 mt-3">
                  <Button
                    variant="primary"
                    size="sm"
                    className="flex-1"
                    onClick={() => {
                      if (window.confirm(`¿Aprobar ${o.order.side} ${o.order.quantity} ${o.order.symbol}?`)) {
                        approve.mutate(o.id)
                      }
                    }}
                    disabled={approve.isPending}
                  >
                    Aprobar
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    className="flex-1"
                    onClick={() => reject.mutate(o.id)}
                    disabled={reject.isPending}
                  >
                    Rechazar
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      {rest.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">Historial reciente</h3>
          <div className="space-y-1">
            {rest.slice(0, 20).map((o) => (
              <div key={o.id} className="flex items-center gap-3 bg-gray-900 rounded-lg px-4 py-2.5 text-sm">
                <Badge variant={o.order.side === 'BUY' ? 'green' : 'red'}>{o.order.side}</Badge>
                <span className="font-medium text-gray-200">{o.order.symbol}</span>
                <span className="text-gray-500">{o.order.quantity} acc.</span>
                <Badge variant={o.status === 'filled' ? 'green' : o.status === 'rejected' ? 'red' : 'gray'}>
                  {statusLabel[o.status] ?? o.status}
                </Badge>
                <span className="text-gray-600 text-xs ml-auto">{fmtAge(o.created_at)}</span>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
