import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost'

const styles: Record<Variant, string> = {
  primary:   'bg-brand-600 hover:bg-brand-700 text-white',
  secondary: 'bg-gray-700 hover:bg-gray-600 text-gray-100',
  danger:    'bg-red-700 hover:bg-red-600 text-white',
  ghost:     'bg-transparent hover:bg-gray-800 text-gray-300',
}

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: 'sm' | 'md'
  children: ReactNode
}

export function Button({ variant = 'secondary', size = 'md', className = '', children, ...rest }: Props) {
  const sz = size === 'sm' ? 'px-3 py-1.5 text-xs' : 'px-4 py-2 text-sm'
  return (
    <button
      className={`inline-flex items-center gap-1.5 rounded font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${styles[variant]} ${sz} ${className}`}
      {...rest}
    >
      {children}
    </button>
  )
}
