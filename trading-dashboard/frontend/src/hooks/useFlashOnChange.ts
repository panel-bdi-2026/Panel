import { useEffect, useRef, useState } from 'react'

/**
 * Devuelve una clase de animación ('flash-up' | 'flash-down' | '') que se
 * dispara ~600ms cuando `value` cambia respecto al valor anterior.
 * Da la sensación "viva" al tickear precios/P&L.
 */
export function useFlashOnChange(value: number | null | undefined): string {
  const prev = useRef<number | null | undefined>(value)
  const [cls, setCls] = useState('')

  useEffect(() => {
    if (value == null || prev.current == null) {
      prev.current = value
      return
    }
    if (value !== prev.current) {
      setCls(value > prev.current ? 'flash-up' : 'flash-down')
      prev.current = value
      const t = setTimeout(() => setCls(''), 600)
      return () => clearTimeout(t)
    }
  }, [value])

  return cls
}
