import { useEffect, useRef, useState, type RefObject } from 'react'

const THRESHOLD = 70   // px de arrastre para disparar el refresh
const MAX_PULL  = 110  // tope visual del indicador

/**
 * Pull-to-refresh táctil simple: solo actúa cuando el contenedor ya está en
 * scrollTop 0 (si no, es un scroll normal). Devuelve la distancia de arrastre
 * actual (para el indicador visual) y si está refrescando.
 */
export function usePullToRefresh(ref: RefObject<HTMLElement | null>, onRefresh: () => Promise<unknown> | void) {
  const [pull, setPull] = useState(0)
  const [refreshing, setRefreshing] = useState(false)
  const startY = useRef<number | null>(null)
  const pulling = useRef(false)

  useEffect(() => {
    const el = ref.current
    if (!el) return

    const onTouchStart = (e: TouchEvent) => {
      if (el.scrollTop <= 0) {
        startY.current = e.touches[0].clientY
        pulling.current = true
      }
    }

    const onTouchMove = (e: TouchEvent) => {
      if (!pulling.current || startY.current == null) return
      const delta = e.touches[0].clientY - startY.current
      if (delta <= 0) { setPull(0); return }
      // Solo interceptamos el scroll nativo una vez que es claramente un pull
      // (evita robarle el gesto a un scroll/tap normal cerca del tope).
      if (delta > 8 && el.scrollTop <= 0) {
        e.preventDefault()
        setPull(Math.min(delta * 0.5, MAX_PULL))
      }
    }

    const onTouchEnd = async () => {
      if (!pulling.current) return
      pulling.current = false
      startY.current = null
      if (pull >= THRESHOLD) {
        setRefreshing(true)
        try { await onRefresh() } finally {
          setRefreshing(false)
        }
      }
      setPull(0)
    }

    el.addEventListener('touchstart', onTouchStart, { passive: true })
    el.addEventListener('touchmove', onTouchMove, { passive: false })
    el.addEventListener('touchend', onTouchEnd)
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref, pull])

  return { pull, refreshing, threshold: THRESHOLD }
}
