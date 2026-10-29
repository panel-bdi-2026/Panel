interface Props {
  className?: string
  count?: number
}

export function Skeleton({ className = 'h-4 w-full', count = 1 }: Props) {
  if (count === 1) return <div className={`animate-pulse bg-surface-2 rounded ${className}`} />
  return (
    <div className="flex flex-col gap-2">
      {Array.from({ length: count }).map((_, i) => (
        <div key={i} className={`animate-pulse bg-surface-2 rounded ${className}`} />
      ))}
    </div>
  )
}
