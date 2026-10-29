import type { HTMLAttributes, ReactNode } from 'react'

interface Props extends HTMLAttributes<HTMLDivElement> {
  children: ReactNode
  padding?: 'none' | 'sm' | 'md'
  interactive?: boolean
}

const pad = { none: '', sm: 'p-3', md: 'p-4' }

export function Card({ children, padding = 'md', interactive = false, className = '', ...rest }: Props) {
  return (
    <div
      className={`bg-surface-1 border border-surface-3 rounded-xl2 shadow-card ${pad[padding]} ${
        interactive ? 'cursor-pointer hover:border-gray-600 hover:bg-surface-2 transition-colors' : ''
      } ${className}`}
      {...rest}
    >
      {children}
    </div>
  )
}
