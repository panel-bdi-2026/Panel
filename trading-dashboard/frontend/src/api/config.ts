import { apiFetch } from './client'

export const fetchRules = () => apiFetch<Record<string, unknown>>('/api/rules')
export const updateRules = (rules: Record<string, unknown>) =>
  apiFetch<Record<string, unknown>>('/api/rules', {
    method: 'PUT',
    body: JSON.stringify({ rules }),
  })

export const fetchScreenerConfig = () =>
  apiFetch<Record<string, unknown>>('/api/signals/config')
export const updateScreenerConfig = (config: Record<string, unknown>) =>
  apiFetch<Record<string, unknown>>('/api/signals/config', {
    method: 'PUT',
    body: JSON.stringify({ config }),
  })

export const fetchStrategies = () =>
  apiFetch<{ id: string; name: string; description: string; supports_backtest: boolean }[]>(
    '/api/strategies',
  )
