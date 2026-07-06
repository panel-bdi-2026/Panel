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
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string>),
  }
  if (key) headers['X-API-Key'] = key

  const res = await fetch(`${BASE}${path}`, { ...init, headers })

  if (res.status === 401) {
    useAuthStore.getState().logout()
    throw new ApiError(401, 'Unauthorized')
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new ApiError(res.status, text)
  }
  if (res.status === 204) return undefined as T
  return res.json()
}
