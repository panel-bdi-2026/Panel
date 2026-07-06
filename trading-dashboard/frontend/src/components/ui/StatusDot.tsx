interface Props { active: boolean; pulse?: boolean; className?: string }

export function StatusDot({ active, pulse = true, className = '' }: Props) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${active ? 'bg-green-400' : 'bg-red-500'} ${active && pulse ? 'animate-pulse' : ''} ${className}`} />
  )
}
