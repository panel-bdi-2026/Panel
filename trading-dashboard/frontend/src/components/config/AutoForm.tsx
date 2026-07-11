import { useState } from 'react'
import { Toggle } from '../ui/Toggle'
import { ChevronDown, ChevronRight } from 'lucide-react'

type Json = string | number | boolean | null | Json[] | { [k: string]: Json }

interface Props {
  value: Record<string, unknown>
  onChange: (v: Record<string, unknown>) => void
  /** claves a omitir (ya se muestran aparte, ej. listas de símbolos) */
  omit?: string[]
}

function humanize(key: string): string {
  return key.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function unitSuffix(key: string): string {
  if (key.endsWith('_pct')) return '%'
  if (key.endsWith('_usd')) return '$'
  if (key.endsWith('_days')) return 'días'
  if (key.endsWith('_minutes')) return 'min'
  if (key.endsWith('_seconds')) return 'seg'
  if (key.endsWith('_ratio') || key.endsWith('_multiplier')) return '×'
  return ''
}

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v)

/**
 * Formulario genérico: booleanos → toggle, números → input numérico con
 * sufijo de unidad inferido del nombre de la clave, strings → input de
 * texto, objetos anidados (ej. overrides por estrategia) → sección
 * colapsable recursiva. Evita tener que hardcodear ~80 campos a mano y
 * cubre automáticamente config nueva que se agregue en el backend.
 */
export function AutoForm({ value, onChange, omit = [] }: Props) {
  const entries = Object.entries(value).filter(([k]) => !omit.includes(k))
  const primitives = entries.filter(([, v]) => !isPlainObject(v) && !Array.isArray(v))
  const nested = entries.filter(([, v]) => isPlainObject(v))
  const arrays = entries.filter(([, v]) => Array.isArray(v))

  const setField = (key: string, v: unknown) => onChange({ ...value, [key]: v })

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-3">
        {primitives.map(([key, v]) => (
          <FieldRow key={key} label={humanize(key)} unit={unitSuffix(key)}>
            {typeof v === 'boolean' ? (
              <Toggle checked={v} onChange={(nv) => setField(key, nv)} />
            ) : typeof v === 'number' ? (
              <input
                type="number"
                value={v}
                onChange={(e) => setField(key, e.target.value === '' ? 0 : parseFloat(e.target.value))}
                className="w-28 bg-surface-2 border border-surface-3 rounded-md px-2 py-1 text-sm text-gray-100 text-right nums focus:outline-none focus:border-brand-500"
              />
            ) : v === null ? (
              <input
                type="text"
                placeholder="—"
                onChange={(e) => setField(key, e.target.value === '' ? null : e.target.value)}
                className="w-28 bg-surface-2 border border-surface-3 rounded-md px-2 py-1 text-sm text-gray-100 focus:outline-none focus:border-brand-500"
              />
            ) : (
              <input
                type="text"
                value={String(v)}
                onChange={(e) => setField(key, e.target.value)}
                className="w-36 bg-surface-2 border border-surface-3 rounded-md px-2 py-1 text-sm text-gray-100 focus:outline-none focus:border-brand-500"
              />
            )}
          </FieldRow>
        ))}
      </div>

      {nested.map(([key, v]) => (
        <NestedSection
          key={key}
          label={humanize(key)}
          value={v as Record<string, unknown>}
          onChange={(nv) => setField(key, nv)}
        />
      ))}

      {arrays.map(([key, v]) => (
        <div key={key} className="text-xs text-gray-600">
          {humanize(key)}: lista de {(v as unknown[]).length} — se edita en JSON avanzado
        </div>
      ))}
    </div>
  )
}

function FieldRow({ label, unit, children }: { label: string; unit: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 bg-surface-2/50 rounded-lg px-3 py-2">
      <span className="text-xs text-gray-400 min-w-0 truncate" title={label}>{label}</span>
      <div className="flex items-center gap-1.5 shrink-0">
        {children}
        {unit && <span className="text-xs text-gray-600 w-5">{unit}</span>}
      </div>
    </div>
  )
}

function NestedSection({
  label, value, onChange,
}: { label: string; value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="border border-surface-3 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-3 py-2 bg-surface-2 hover:bg-surface-3/60 transition-colors text-left"
      >
        <span className="text-xs font-medium text-gray-300">{label}</span>
        {open ? <ChevronDown size={14} className="text-gray-500" /> : <ChevronRight size={14} className="text-gray-500" />}
      </button>
      {open && (
        <div className="p-3">
          <AutoForm value={value} onChange={(nv) => onChange(nv as Record<string, Json>)} />
        </div>
      )}
    </div>
  )
}
