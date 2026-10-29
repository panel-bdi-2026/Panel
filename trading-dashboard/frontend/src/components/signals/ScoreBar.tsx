interface Props {
  score: number
  max?: number
  label?: string
}

export function ScoreBar({ score, max = 10, label }: Props) {
  const pct = Math.min(100, Math.max(0, (score / max) * 100))
  const color =
    pct >= 75 ? 'bg-green-500' :
    pct >= 50 ? 'bg-blue-500' :
    pct >= 25 ? 'bg-yellow-500' :
    'bg-gray-600'

  return (
    <div className="flex items-center gap-2 w-full">
      <div className="flex-1 h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {label !== undefined ? (
        <span className="text-xs text-gray-400 w-8 text-right shrink-0">{label}</span>
      ) : (
        <span className="text-xs text-gray-400 w-8 text-right shrink-0">{score.toFixed(1)}</span>
      )}
    </div>
  )
}
