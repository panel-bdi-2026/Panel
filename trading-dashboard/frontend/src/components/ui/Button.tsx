import type { ButtonHTMLAttributes, ReactNode } from 'react'

type Variant = 'primary' | 'secondary' | 'danger' | 'ghost'
type Size    = 'xs' | 'sm' | 'md' | 'lg'

const variantStyles: Record<Variant, string> = {
  primary:   'bg-brand-600 hover:bg-brand-500 active:bg-brand-700 text-white shadow-sm',
  secondary: 'bg-gray-700 hover:bg-gray-600 active:bg-gray-800 text-gray-100',
  danger:    'bg-red-700 hover:bg-red-600 active:bg-red-800 text-white shadow-sm',
  ghost:     'bg-transparent hover:bg-gray-800 active:bg-gray-900 text-gray-300',
}

const sizeStyles: Record<Size, string> = {
  xs: 'px-2 py-1 text-xs rounded',
  sm: 'px-3 py-1.5 text-xs rounded-md',
  md: 'px-4 py-2 text-sm rounded-lg',
  lg: 'px-5 py-2.5 text-sm rounded-lg',
}

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  children: ReactNode
}

export function Button({ variant = 'secondary', size = 'md', className = '', children, ...rest }: Props) {
  return (
    <button
      className={`inline-flex items-center justify-center gap-1.5 font-medium transition-colors
        disabled:opacity-40 disabled:cursor-not-allowed select-none
        ${variantStyles[variant]} ${sizeStyles[size]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  )
}
