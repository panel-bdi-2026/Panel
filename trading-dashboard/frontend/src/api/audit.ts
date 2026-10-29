import { apiFetch } from './client'
import type { AuditEntry } from './types'

export const fetchAudit = (limit = 100) =>
  apiFetch<AuditEntry[]>(`/api/audit?limit=${limit}`)
