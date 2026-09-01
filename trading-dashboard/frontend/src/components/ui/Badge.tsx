import type { ReactNode } from 'react'

type Variant = 'green' | 'red' | 'yellow' | 'blue' | 'gray'

const styles: Record<Variant, string> = {
  green:  'bg-green-900/50 text-green-300 ring-1 ring-green-700/50',
  red:    'bg-red-900/50 text-red-300 ring-1 ring-red-700/50',
  yellow: 'bg-yellow-900/50 text-yellow-300 ring-1 ring-yellow-700/50',
  blue:   'bg-blue-900/50 text-blue-300 ring-1 ring-blue-700/50',
  gray:   'bg-gray-800 text-gray-400 ring-1 ring-gray-700',
}

interface Props { children: ReactNode; variant?: Variant; className?: string }

export function Badge({ children, variant = 'gray', className = '' }: Props) {
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${styles[variant]} ${className}`}>
      {children}
    </span>
  )
}
