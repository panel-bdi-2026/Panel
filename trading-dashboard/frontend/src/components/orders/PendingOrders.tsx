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

  if (isLoading) return <div className="text-gray-500 text-sm">Cargando órdenes…</div>
  if (!orders?.length) return <div className="text-gray-500 text-sm">Sin órdenes pendientes.</div>

  const pending = orders.filter((o) => o.status === 'pending')
  const rest = orders.filter((o) => o.status !== 'pending')

  return (
    <div className="space-y-6">
      {pending.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">Pendientes de aprobación</h3>
          <div className="space-y-2">
            {pending.map((o) => (
              <div key={o.id} className="flex items-center gap-4 bg-gray-900 border border-yellow-700/40 rounded-xl px-4 py-3">
                <Badge variant={o.side === 'BUY' ? 'green' : 'red'}>{o.side}</Badge>
                <span className="font-semibold text-gray-100 w-16">{o.symbol}</span>
                <span className="text-gray-400 text-sm">{o.quantity} acc.</span>
                <span className="text-gray-400 text-sm">{o.order_type}</span>
                {o.limit_price && <span className="text-gray-400 text-sm">Lim {fmtUsd(o.limit_price)}</span>}
                {o.score && <span className="text-gray-500 text-xs">Score {o.score.toFixed(2)}</span>}
                {o.notes && <span className="text-gray-500 text-xs truncate max-w-xs">{o.notes}</span>}
                <span className="text-gray-600 text-xs ml-auto">{fmtAge(o.created_at)}</span>
                <Button
                  variant="primary"
                  size="sm"
                  onClick={() => {
                    if (window.confirm(`¿Aprobar ${o.side} ${o.quantity} ${o.symbol}?`)) {
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
                  onClick={() => reject.mutate(o.id)}
                  disabled={reject.isPending}
                >
                  Rechazar
                </Button>
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
              <div key={o.id} className="flex items-center gap-4 bg-gray-900 rounded-lg px-4 py-2 text-sm opacity-60">
                <Badge variant={o.side === 'BUY' ? 'green' : 'red'}>{o.side}</Badge>
                <span className="font-medium text-gray-200 w-16">{o.symbol}</span>
                <span className="text-gray-400">{o.quantity} acc.</span>
                <Badge variant={o.status === 'filled' ? 'green' : o.status === 'rejected' ? 'red' : 'gray'}>
                  {o.status}
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
