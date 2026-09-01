import { useMemo } from 'react'
import type { FundTrade } from '../../api/types'
import { fmtUsd } from '../../lib/format'

interface Props {
  trades: FundTrade[]
}

interface StatCard {
  label: string
  value: string
  sub?: string
  color: string
}

export function TradeStatsPanel({ trades }: Props) {
  const stats = useMemo<StatCard[]>(() => {
    const closed = trades.filter((t) => t.side === 'SELL' && t.realized_pnl != null)
    if (closed.length === 0) return []

    const wins = closed.filter((t) => (t.realized_pnl ?? 0) > 0)
    const losses = closed.filter((t) => (t.realized_pnl ?? 0) <= 0)
    const winRate = (wins.length / closed.length) * 100

    const sumWins = wins.reduce((s, t) => s + (t.realized_pnl ?? 0), 0)
    const sumLosses = losses.reduce((s, t) => s + (t.realized_pnl ?? 0), 0)
    const avgWin = wins.length > 0 ? sumWins / wins.length : 0
    const avgLoss = losses.length > 0 ? sumLosses / losses.length : 0
    const absSumLosses = Math.abs(sumLosses)
    const profitFactor = absSumLosses > 0 ? sumWins / absSumLosses : null

    const largestWin = wins.length > 0 ? Math.max(...wins.map((t) => t.realized_pnl ?? 0)) : null
    const largestLoss = losses.length > 0 ? Math.min(...losses.map((t) => t.realized_pnl ?? 0)) : null

    const expectancy = (winRate / 100) * avgWin + (1 - winRate / 100) * avgLoss

    return [
      {
        label: 'Trades cerrados',
        value: closed.length.toString(),
        sub: `${wins.length}G / ${losses.length}P`,
        color: 'text-gray-200',
      },
      {
        label: 'Win rate',
        value: `${winRate.toFixed(1)}%`,
        sub: winRate >= 50 ? 'Por encima del 50%' : 'Por debajo del 50%',
        color: winRate >= 50 ? 'text-green-400' : 'text-red-400',
      },
      {
        label: 'Prom. ganancia',
        value: fmtUsd(avgWin),
        sub: wins.length > 0 ? `${wins.length} trades` : '—',
        color: 'text-green-400',
      },
      {
        label: 'Prom. pérdida',
        value: fmtUsd(avgLoss),
        sub: losses.length > 0 ? `${losses.length} trades` : '—',
        color: 'text-red-400',
      },
      {
        label: 'Profit factor',
        value: profitFactor != null ? profitFactor.toFixed(2) : '∞',
        sub: profitFactor != null && profitFactor >= 1 ? 'Positivo' : profitFactor != null ? 'Negativo' : 'Sin pérdidas',
        color: profitFactor == null || profitFactor >= 1 ? 'text-blue-400' : 'text-red-400',
      },
      {
        label: 'Expectativa/trade',
        value: fmtUsd(expectancy),
        sub: expectancy >= 0 ? 'Edge positivo' : 'Edge negativo',
        color: expectancy >= 0 ? 'text-blue-400' : 'text-red-400',
      },
      {
        label: 'Mayor ganancia',
        value: largestWin != null ? fmtUsd(largestWin) : '—',
        color: 'text-green-400',
      },
      {
        label: 'Mayor pérdida',
        value: largestLoss != null ? fmtUsd(largestLoss) : '—',
        color: 'text-red-400',
      },
    ]
  }, [trades])

  if (stats.length === 0) {
    return (
      <div className="h-32 flex flex-col items-center justify-center text-gray-600 text-sm gap-1">
        <span>Sin trades cerrados aún</span>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 py-1">
      {stats.map((s) => (
        <div key={s.label} className="bg-gray-800 rounded-lg px-3 py-2.5">
          <p className="text-xs text-gray-500 mb-1 leading-tight">{s.label}</p>
          <p className={`font-semibold nums text-sm ${s.color}`}>{s.value}</p>
          {s.sub && <p className="text-xs text-gray-600 mt-0.5 leading-tight">{s.sub}</p>}
        </div>
      ))}
    </div>
  )
}
