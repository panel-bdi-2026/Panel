import { ArrowUp, ArrowDown } from 'lucide-react'

interface Props {
  /** valor principal ya formateado (ej. "+$176.80") */
  value: string
  /** signo que determina el color/flecha; si no se pasa se infiere de `raw` */
  raw?: number
  positive?: boolean
  size?: 'sm' | 'md' | 'lg'
  showArrow?: boolean
  className?: string
}

const sizeCls = { sm: 'text-xs', md: 'text-sm', lg: 'text-base' }

export function MetricDelta({ value, raw, positive, size = 'md', showArrow = true, className = '' }: Props) {
  const isUp = positive ?? ((raw ?? 0) >= 0)
  const color = isUp ? 'text-profit' : 'text-loss'
  const Arrow = isUp ? ArrowUp : ArrowDown
  return (
    <span className={`inline-flex items-center gap-0.5 nums font-semibold ${color} ${sizeCls[size]} ${className}`}>
      {showArrow && <Arrow size={size === 'lg' ? 16 : 13} className="shrink-0" />}
      {value}
    </span>
  )
}
