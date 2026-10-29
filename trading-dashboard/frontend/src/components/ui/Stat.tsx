import type { ReactNode } from 'react'

interface Props {
  label: string
  value: ReactNode
  sub?: ReactNode
  valueClass?: string
  align?: 'left' | 'right'
}

export function Stat({ label, value, sub, valueClass = 'text-gray-100', align = 'left' }: Props) {
  return (
    <div className={`flex flex-col shrink-0 ${align === 'right' ? 'items-end text-right' : ''}`}>
      <span className="text-[11px] uppercase tracking-wide text-gray-500 whitespace-nowrap">{label}</span>
      <span className={`text-sm font-semibold nums whitespace-nowrap ${valueClass}`}>{value}</span>
      {sub && <span className="text-[11px] text-gray-500 nums">{sub}</span>}
    </div>
  )
}
