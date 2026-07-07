import { useAuthStore } from '../store/auth'

const BASE = ''  // same origin

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const key = useAuthStore.getState().apiKey
  const isGet = !init.method || init.method.toUpperCase() === 'GET'
  const headers: Record<string, string> = {
    ...(isGet ? {} : { 'Content-Type': 'application/json' }),
    ...(init.headers as Record<string, string>),
  }
  if (key) headers['X-API-Key'] = key

  const res = await fetch(`${BASE}${path}`, { ...init, headers })

  if (res.status === 401) {
    useAuthStore.getState().logout()
    throw new ApiError(401, 'Unauthorized')
  }
  if (!res.ok) {
    const ct = res.headers.get('content-type') ?? ''
    const text = ct.includes('application/json')
      ? await res.json().then((d) => d?.detail ?? d?.message ?? JSON.stringify(d)).catch(() => res.statusText)
      : await res.text().then((t) => t.slice(0, 200)).catch(() => res.statusText)
    throw new ApiError(res.status, text)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}
