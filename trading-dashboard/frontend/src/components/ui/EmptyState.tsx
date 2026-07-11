import type { ReactNode } from 'react'

interface Props {
  icon?: ReactNode
  title: string
  subtitle?: string
  action?: ReactNode
}

export function EmptyState({ icon, title, subtitle, action }: Props) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {icon && <div className="text-3xl mb-2 opacity-80">{icon}</div>}
      <p className="text-gray-300 font-medium">{title}</p>
      {subtitle && <p className="text-gray-600 text-sm mt-1 max-w-xs">{subtitle}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}
