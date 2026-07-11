import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchPendingOrders, approveOrder, rejectOrder } from '../../api/orders'
import { fmtUsd, fmtAge } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { Card } from '../ui/Card'
import { Skeleton } from '../ui/Skeleton'
import { EmptyState } from '../ui/EmptyState'
import { useToastStore } from '../ui/Toast'
import { CheckCircle2, XCircle, Clock, CircleSlash } from 'lucide-react'
import type { PendingOrder } from '../../api/types'

const ORDER_TYPE_ES: Record<string, string> = {
  MKT: 'a Mercado', MARKET: 'a Mercado', LMT: 'Límite', LIMIT: 'Límite', STP: 'Stop',
}

// Descripción en lenguaje natural (estilo IBKR: "Compra 85 WYFI a Mercado")
function describe(o: PendingOrder): string {
  const verb = o.order.side === 'BUY' ? 'Compra' : 'Venta'
  const type = ORDER_TYPE_ES[o.order.order_type?.toUpperCase()] ?? o.order.order_type
  const at = o.order.limit_price ? ` @ ${fmtUsd(o.order.limit_price)}` : ''
  return `${verb} ${o.order.quantity} ${o.order.symbol} ${type}${at}`
}

const STATUS: Record<string, { label: string; Icon: typeof CheckCircle2; cls: string }> = {
  filled:    { label: 'Ejecutada', Icon: CheckCircle2, cls: 'text-profit' },
  rejected:  { label: 'Rechazada', Icon: XCircle,      cls: 'text-loss' },
  cancelled: { label: 'Cancelada', Icon: CircleSlash,  cls: 'text-gray-500' },
  expired:   { label: 'Expirada',  Icon: CircleSlash,  cls: 'text-gray-500' },
  pending:   { label: 'Pendiente', Icon: Clock,        cls: 'text-warn' },
}

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

  if (isLoading) return <Skeleton className="h-16 rounded-xl2" count={3} />
  if (!orders?.length) return <EmptyState icon="📋" title="Sin órdenes pendientes" subtitle="Las órdenes generadas por el sistema o creadas manualmente aparecerán acá." />

  const pending = orders.filter((o) => o.status === 'pending')
  const rest = orders.filter((o) => o.status !== 'pending')

  return (
    <div className="space-y-6">
      {pending.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">
            Pendientes de aprobación ({pending.length})
          </h3>
          <div className="space-y-2">
            {pending.map((o) => (
              <Card key={o.id} padding="sm" className="border-warn/30">
                <div className="flex items-start gap-3">
                  <Clock size={18} className="text-warn shrink-0 mt-0.5" />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="font-semibold text-gray-100">{describe(o)}</span>
                      <span className="text-gray-600 text-xs ml-auto shrink-0">{fmtAge(o.created_at)}</span>
                    </div>
                    {o.decision?.estimated_value_usd != null && (
                      <p className="text-xs text-gray-500 nums mt-0.5">≈ {fmtUsd(o.decision.estimated_value_usd)}</p>
                    )}
                    {o.decision?.violations?.map((v, i) => (
                      <p key={i} className="text-xs text-loss mt-0.5 break-words">⚠ {v}</p>
                    ))}
                    {o.notes && <p className="text-gray-500 text-xs mt-1 line-clamp-2">{o.notes}</p>}
                  </div>
                </div>
                <div className="flex gap-2 mt-3">
                  <Button
                    variant="primary" size="sm" className="flex-1"
                    onClick={() => { if (window.confirm(`¿Aprobar ${describe(o)}?`)) approve.mutate(o.id) }}
                    disabled={approve.isPending}
                  >Aprobar</Button>
                  <Button
                    variant="danger" size="sm" className="flex-1"
                    onClick={() => reject.mutate(o.id)} disabled={reject.isPending}
                  >Rechazar</Button>
                </div>
              </Card>
            ))}
          </div>
        </section>
      )}

      {rest.length > 0 && (
        <section>
          <h3 className="text-xs font-semibold text-gray-500 uppercase mb-3">Historial reciente</h3>
          <div className="space-y-1">
            {rest.slice(0, 20).map((o) => {
              const st = STATUS[o.status] ?? STATUS.pending
              return (
                <div key={o.id} className="flex items-center gap-3 bg-surface-1 border border-surface-3 rounded-lg px-4 py-2.5 text-sm">
                  <st.Icon size={16} className={`${st.cls} shrink-0`} />
                  <span className="text-gray-300 truncate">{describe(o)}</span>
                  <Badge variant={o.status === 'filled' ? 'green' : o.status === 'rejected' ? 'red' : 'gray'}>
                    {st.label}
                  </Badge>
                  <span className="text-gray-600 text-xs ml-auto nums shrink-0">{fmtAge(o.created_at)}</span>
                </div>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}
