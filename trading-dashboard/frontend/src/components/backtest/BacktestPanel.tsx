import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { runBacktest, runWalkForward } from '../../api/backtest'
import { fetchStrategies } from '../../api/config'
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ReferenceLine,
} from 'recharts'
import { Button } from '../ui/Button'
import { fmtPct } from '../../lib/format'
import type { BacktestSummary, WalkForwardResult } from '../../api/backtest'

function MetricCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="bg-gray-800 rounded-lg px-3 py-2">
      <p className="text-xs text-gray-500">{label}</p>
      <p className="text-sm font-semibold text-gray-100 mt-0.5">{value}</p>
      {sub && <p className="text-xs text-gray-500">{sub}</p>}
    </div>
  )
}

function fmt(v: number) {
  return `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`
}

function BacktestResults({ data }: { data: BacktestSummary }) {
  const dsr = data.deflated_sharpe_ratio_pct
  const dsrColor = dsr == null ? 'gray' : dsr >= 95 ? 'green' : dsr >= 70 ? 'yellow' : 'red'
  const pf = data.profit_factor_is_infinite ? '∞' : data.profit_factor?.toFixed(2) ?? '—'

  const curvePoints = data.equity_curve?.map((p) => ({
    date: p.date.slice(0, 10),
    Strategy: p.equity_pct,
  })) ?? []

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <MetricCard label="Retorno total" value={fmt(data.strategy_cumulative_return_pct)} sub={`Bench: ${fmt(data.benchmark_cumulative_return_pct)}`} />
        <MetricCard label="Sharpe" value={data.sharpe_ratio?.toFixed(2) ?? '—'} />
        <MetricCard label="Max drawdown" value={`-${data.max_drawdown_pct.toFixed(1)}%`} />
        <MetricCard
          label="DSR"
          value={dsr != null ? `${dsr.toFixed(1)}%` : '—'}
          sub={dsr != null ? (dsrColor === 'green' ? 'Bueno' : dsrColor === 'yellow' ? 'Marginal' : 'Bajo') : undefined}
        />
        <MetricCard label="Win rate" value={fmtPct(data.win_rate_pct)} />
        <MetricCard label="Expectancy" value={fmt(data.expectancy_pct)} />
        <MetricCard label="Profit factor" value={pf} />
        <MetricCard label="Exposición avg" value={fmtPct(data.avg_exposure_pct)} />
        <MetricCard label="N° trades" value={String(data.total_trades)} />
        <MetricCard label="Avg win" value={fmt(data.avg_win_pct)} />
        <MetricCard label="Avg loss" value={fmt(data.avg_loss_pct)} />
        <MetricCard label="Alpha avg" value={data.avg_alpha_pct != null ? fmt(data.avg_alpha_pct) : '—'} />
      </div>

      {curvePoints.length > 0 && (
        <div className="h-48">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={curvePoints} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="date" tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} interval="preserveStartEnd" />
              <YAxis tick={{ fill: '#6b7280', fontSize: 10 }} tickLine={false} tickFormatter={fmt} width={52} />
              <Tooltip contentStyle={{ background: '#111827', border: '1px solid #374151' }} />
              <ReferenceLine y={0} stroke="#374151" />
              <Line type="monotone" dataKey="Strategy" stroke="#3b82f6" dot={false} strokeWidth={2} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {Object.keys(data.exit_reason_counts).length > 0 && (
        <div>
          <p className="text-xs text-gray-500 uppercase mb-2">Motivo de salida</p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(data.exit_reason_counts).map(([k, v]) => (
              <span key={k} className="text-xs bg-gray-800 px-2 py-1 rounded text-gray-400">
                {k}: <span className="text-gray-200 font-medium">{v}</span>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function WalkForwardResults({ data }: { data: WalkForwardResult }) {
  return (
    <div className="space-y-3">
      <p className="text-sm text-gray-400">
        Folds positivos: <span className="text-green-400 font-semibold">{data.n_positive_folds}/{data.folds.length}</span>
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-800">
              <th className="text-left pb-2 pr-3">Período</th>
              <th className="text-right pb-2 pr-3">Retorno</th>
              <th className="text-right pb-2 pr-3">Bench</th>
              <th className="text-right pb-2 pr-3">Sharpe</th>
              <th className="text-right pb-2 pr-3">DD máx</th>
              <th className="text-right pb-2">Trades</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-800">
            {data.folds.map((f) => {
              const pos = f.strategy_cumulative_return_pct >= 0
              return (
                <tr key={f.fold} className={pos ? '' : 'opacity-70'}>
                  <td className="py-1.5 pr-3 text-gray-400">
                    {f.start_date.slice(0, 10)} → {f.end_date.slice(0, 10)}
                  </td>
                  <td className={`py-1.5 pr-3 text-right font-semibold ${pos ? 'text-green-400' : 'text-red-400'}`}>
                    {fmt(f.strategy_cumulative_return_pct)}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-gray-400">
                    {fmt(f.benchmark_cumulative_return_pct)}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-gray-300">
                    {f.sharpe_ratio?.toFixed(2) ?? '—'}
                  </td>
                  <td className="py-1.5 pr-3 text-right text-gray-300">
                    -{f.max_drawdown_pct.toFixed(1)}%
                  </td>
                  <td className="py-1.5 text-right text-gray-300">{f.total_trades}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export function BacktestPanel() {
  const [strategyId, setStrategyId] = useState<string | undefined>()
  const [mode, setMode] = useState<'backtest' | 'walkforward'>('backtest')
  const [nFolds, setNFolds] = useState(3)

  const { data: strategies } = useQuery({
    queryKey: ['strategies'],
    queryFn: fetchStrategies,
  })

  const backtestMutation = useMutation({
    mutationFn: () => runBacktest(strategyId),
  })

  const wfMutation = useMutation({
    mutationFn: () => runWalkForward(strategyId, nFolds),
  })

  const isRunning = backtestMutation.isPending || wfMutation.isPending
  const backtestableStrategies = strategies?.filter((s) => s.supports_backtest) ?? []

  return (
    <div className="space-y-4">
      {/* Controls */}
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-xs text-gray-500 mb-1">Estrategia</label>
          <select
            value={strategyId ?? ''}
            onChange={(e) => setStrategyId(e.target.value || undefined)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none"
          >
            <option value="">Default (config actual)</option>
            {backtestableStrategies.map((s) => (
              <option key={s.id} value={s.id}>{s.name}</option>
            ))}
          </select>
        </div>

        <div className="flex rounded-lg overflow-hidden border border-gray-700">
          {(['backtest', 'walkforward'] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-4 py-2 text-sm transition-colors ${
                mode === m ? 'bg-brand-600 text-white' : 'bg-gray-800 text-gray-400 hover:bg-gray-700'
              }`}
            >
              {m === 'backtest' ? 'Backtest' : 'Walk-forward'}
            </button>
          ))}
        </div>

        {mode === 'walkforward' && (
          <div>
            <label className="block text-xs text-gray-500 mb-1">Folds</label>
            <select
              value={nFolds}
              onChange={(e) => setNFolds(Number(e.target.value))}
              className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 focus:outline-none"
            >
              {[2, 3, 4, 5].map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </div>
        )}

        <Button
          variant="primary"
          onClick={() => mode === 'backtest' ? backtestMutation.mutate() : wfMutation.mutate()}
          disabled={isRunning}
        >
          {isRunning ? 'Calculando… (puede tardar varios minutos)' : '▶ Correr'}
        </Button>
      </div>

      {/* Error */}
      {(backtestMutation.error || wfMutation.error) && (
        <div className="bg-red-950 border border-red-800 rounded-lg px-4 py-3 text-sm text-red-300">
          {(backtestMutation.error as Error)?.message || (wfMutation.error as Error)?.message}
        </div>
      )}

      {/* Results */}
      {backtestMutation.data && mode === 'backtest' && (
        <BacktestResults data={backtestMutation.data} />
      )}
      {wfMutation.data && mode === 'walkforward' && (
        <WalkForwardResults data={wfMutation.data} />
      )}

      {!backtestMutation.data && !wfMutation.data && !isRunning && (
        <p className="text-gray-600 text-sm text-center py-12">
          Seleccioná una estrategia y apretá "Correr" para iniciar el backtest.
        </p>
      )}
    </div>
  )
}
