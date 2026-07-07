import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchFunds } from '../../api/funds'
import { createOrder, fetchSizeSuggestion } from '../../api/orders'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { useToastStore } from '../ui/Toast'

interface Props { open: boolean; onClose: () => void; initialSymbol?: string }

const EMPTY = {
  symbol: '',
  side: 'BUY' as 'BUY' | 'SELL',
  quantity: '',
  order_type: 'MKT' as 'MKT' | 'LMT',
  limit_price: '',
  stop_loss_price: '',
  fund_id: '',
}

export function NewOrderForm({ open, onClose, initialSymbol }: Props) {
  const [form, setForm] = useState(EMPTY)

  useEffect(() => {
    if (open) setForm({ ...EMPTY, symbol: initialSymbol ?? '' })
  }, [open, initialSymbol])
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: funds } = useQuery({ queryKey: ['funds'], queryFn: fetchFunds })

  const { data: suggestion, isLoading: loadingSuggestion } = useQuery({
    queryKey: ['size-suggestion', form.symbol, form.fund_id],
    queryFn: () => fetchSizeSuggestion(form.symbol, form.fund_id),
    enabled: form.symbol.length >= 2 && form.side === 'BUY' && form.fund_id !== '',
    staleTime: 30000,
  })

  const submit = useMutation({
    mutationFn: () =>
      createOrder({
        symbol: form.symbol.toUpperCase(),
        side: form.side,
        quantity: Number(form.quantity),
        order_type: form.order_type,
        limit_price: form.limit_price ? Number(form.limit_price) : null,
        stop_loss_price: form.stop_loss_price ? Number(form.stop_loss_price) : null,
        fund_id: form.fund_id || undefined,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['orders'] })
      addToast('Orden enviada para revisión', 'success')
      setForm(EMPTY)
      onClose()
    },
    onError: (e: Error) => addToast(e.message || 'Error al enviar orden', 'error'),
  })

  const set = (k: keyof typeof EMPTY, v: string) => setForm((f) => ({ ...f, [k]: v }))

  const canSubmit =
    form.symbol.trim().length >= 1 &&
    Number(form.quantity) > 0 &&
    (form.order_type === 'MKT' || Number(form.limit_price) > 0)

  return (
    <Modal open={open} onClose={onClose} title="Nueva orden">
      <div className="space-y-4">
        {/* Symbol + Side */}
        <div className="flex gap-3">
          <div className="flex-1">
            <label className="block text-xs text-gray-500 mb-1">Símbolo</label>
            <input
              autoFocus
              value={form.symbol}
              onChange={(e) => set('symbol', e.target.value.toUpperCase())}
              placeholder="AAPL"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-brand-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Lado</label>
            <div className="flex rounded-lg overflow-hidden border border-gray-700">
              {(['BUY', 'SELL'] as const).map((s) => (
                <button
                  key={s}
                  onClick={() => set('side', s)}
                  className={`px-4 py-2 text-sm font-semibold transition-colors ${
                    form.side === s
                      ? s === 'BUY' ? 'bg-green-700 text-white' : 'bg-red-700 text-white'
                      : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Fondo */}
        <div>
          <label className="block text-xs text-gray-500 mb-1">Fondo (opcional)</label>
          <select
            value={form.fund_id}
            onChange={(e) => set('fund_id', e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none"
          >
            <option value="">Sin fondo (cuenta general)</option>
            {funds?.filter((f) => !f.closed).map((f) => (
              <option key={f.id} value={f.id}>{f.name}</option>
            ))}
          </select>
        </div>

        {/* Quantity + sugerencia */}
        <div>
          <label className="block text-xs text-gray-500 mb-1">
            Cantidad
            {form.side === 'BUY' && form.symbol.length >= 2 && form.fund_id === '' && (
              <span className="ml-2 text-gray-600">selecciona un fondo para sugerencia</span>
            )}
            {suggestion && !loadingSuggestion && (
              <button
                className="ml-2 text-blue-400 hover:text-blue-300"
                onClick={() => set('quantity', String(suggestion.quantity))}
              >
                sugerido: {suggestion.quantity}
              </button>
            )}
            {loadingSuggestion && <span className="ml-2 text-gray-600">calculando…</span>}
          </label>
          <input
            type="number"
            min={1}
            value={form.quantity}
            onChange={(e) => set('quantity', e.target.value)}
            placeholder="10"
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-brand-500"
          />
        </div>

        {/* Tipo + precio */}
        <div className="flex gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Tipo</label>
            <div className="flex rounded-lg overflow-hidden border border-gray-700">
              {(['MKT', 'LMT'] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => set('order_type', t)}
                  className={`px-4 py-2 text-sm transition-colors ${
                    form.order_type === t
                      ? 'bg-brand-600 text-white'
                      : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
          {form.order_type === 'LMT' && (
            <div className="flex-1">
              <label className="block text-xs text-gray-500 mb-1">Precio límite</label>
              <input
                type="number"
                step="0.01"
                value={form.limit_price}
                onChange={(e) => set('limit_price', e.target.value)}
                placeholder="150.00"
                className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none focus:border-brand-500"
              />
            </div>
          )}
        </div>

        {/* Stop loss */}
        {form.side === 'BUY' && (
          <div>
            <label className="block text-xs text-gray-500 mb-1">
              Stop loss (opcional)
              {suggestion?.stop_price && (
                <button
                  className="ml-2 text-blue-400 hover:text-blue-300"
                  onClick={() => set('stop_loss_price', String(suggestion.stop_price))}
                >
                  sugerido: ${suggestion.stop_price}
                </button>
              )}
            </label>
            <input
              type="number"
              step="0.01"
              value={form.stop_loss_price}
              onChange={(e) => set('stop_loss_price', e.target.value)}
              placeholder="140.00"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none"
            />
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button variant="secondary" onClick={onClose}>Cancelar</Button>
          <Button
            variant="primary"
            onClick={() => submit.mutate()}
            disabled={!canSubmit || submit.isPending}
          >
            {submit.isPending ? 'Enviando…' : 'Enviar orden'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
