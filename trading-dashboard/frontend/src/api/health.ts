import { useAuthStore } from '../store/auth'

export type HealthIssue =
  | 'ibkr_disconnected'
  | 'market_data_degraded'
  | 'trading_halted'
  | 'scan_stale'
  | string

export interface Health {
  status: 'ok' | 'degraded'
  issues: HealthIssue[]
  timestamp: string
  mode: 'paper' | 'live'
  halted: boolean
  uptime_seconds: number
  last_scan_at: string | null
}

// /api/health devuelve 200 cuando está sano y 503 (con el payload en `detail`)
// cuando está degradado. No usamos apiFetch porque ese lanza en 503; acá
// queremos leer el payload en ambos casos.
export async function fetchHealth(): Promise<Health> {
  const key = useAuthStore.getState().apiKey
  const res = await fetch('/api/health', {
    headers: key ? { 'X-API-Key': key } : {},
  })
  const body = await res.json().catch(() => null)
  // Cuando FastAPI hace raise HTTPException(503, detail=payload), el payload
  // viaja en body.detail; cuando responde 200, es el body directo.
  const data = (body && (body.detail ?? body)) as Health
  return data
}
