import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchRules, updateRules, fetchScreenerConfig, updateScreenerConfig } from '../../api/config'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { useToastStore } from '../ui/Toast'
import { AutoForm } from './AutoForm'
import { Code2, SlidersHorizontal } from 'lucide-react'

type Tab = 'rules' | 'screener'
type Mode = 'form' | 'json'

const SYMBOL_LIST_KEYS = ['symbol_whitelist', 'universe']

function splitSymbolLists(obj: Record<string, unknown>) {
  const lists: Record<string, string[]> = {}
  const rest: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(obj)) {
    if (SYMBOL_LIST_KEYS.includes(k) && Array.isArray(v)) {
      lists[k] = v as string[]
    } else {
      rest[k] = v
    }
  }
  return { lists, rest }
}

interface JsonEditorProps {
  initialValue: Record<string, unknown>
  onChange: (v: Record<string, unknown>) => void
  onError: (e: string | null) => void
}

// key-resettable editor: does NOT re-sync from parent on edits.
// Only remounts when key changes (i.e., when server data changes).
function JsonEditor({ initialValue, onChange, onError }: JsonEditorProps) {
  const [text, setText] = useState(() => JSON.stringify(initialValue, null, 2))
  const [localError, setLocalError] = useState<string | null>(null)

  const handleChange = (v: string) => {
    setText(v)
    try {
      const parsed = JSON.parse(v)
      setLocalError(null)
      onError(null)
      onChange(parsed)
    } catch {
      const msg = 'JSON inválido'
      setLocalError(msg)
      onError(msg)
    }
  }

  return (
    <div>
      <textarea
        value={text}
        onChange={(e) => handleChange(e.target.value)}
        className="w-full h-52 sm:h-80 bg-surface-0 border border-surface-3 rounded-lg p-3 text-xs font-mono text-gray-300 focus:outline-none focus:border-brand-500 resize-none"
        spellCheck={false}
      />
      {localError && <p className="text-red-400 text-xs mt-1">{localError}</p>}
    </div>
  )
}

function SymbolListSection({ label, symbols }: { label: string; symbols: string[] }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="border border-surface-3 rounded-lg overflow-hidden">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-2 bg-surface-2 hover:bg-surface-3/60 transition-colors text-left"
      >
        <span className="text-xs font-medium text-gray-400">{label}</span>
        <div className="flex items-center gap-2">
          <span className="bg-surface-3 text-gray-300 text-xs px-2 py-0.5 rounded-full">
            {symbols.length} símbolos
          </span>
          <span className="text-gray-600 text-xs">{expanded ? '▲' : '▼'}</span>
        </div>
      </button>
      {expanded && (
        <div className="px-3 py-2 bg-surface-0 max-h-48 overflow-y-auto">
          <p className="text-xs text-gray-600 mb-2">Solo lectura — editar en JSON avanzado</p>
          <div className="flex flex-wrap gap-1">
            {symbols.map((s) => (
              <span key={s} className="text-xs bg-surface-2 text-gray-400 px-1.5 py-0.5 rounded font-mono">
                {s}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

interface Props { open: boolean; onClose: () => void }

export function ConfigModal({ open, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('rules')
  const [mode, setMode] = useState<Mode>('form')
  const [rulesEdited, setRulesEdited] = useState<Record<string, unknown> | null>(null)
  const [screenerEdited, setScreenerEdited] = useState<Record<string, unknown> | null>(null)
  const [jsonError, setJsonError] = useState<string | null>(null)
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: rulesData } = useQuery({
    queryKey: ['rules'],
    queryFn: fetchRules,
    enabled: open,
  })
  const { data: screenerData } = useQuery({
    queryKey: ['screener-config'],
    queryFn: fetchScreenerConfig,
    enabled: open,
  })

  const rulesSplit = useMemo(
    () => (rulesData ? splitSymbolLists(rulesData) : null),
    [rulesData],
  )
  const screenerSplit = useMemo(
    () => (screenerData ? splitSymbolLists(screenerData) : null),
    [screenerData],
  )

  const saveRules = useMutation({
    mutationFn: () =>
      updateRules({ ...(rulesSplit?.lists ?? {}), ...(rulesEdited ?? rulesData!) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['rules'] })
      addToast('Reglas guardadas', 'success')
      setRulesEdited(null)
    },
    onError: (e: Error) => addToast(e.message || 'Error al guardar reglas', 'error'),
  })

  const saveScreener = useMutation({
    mutationFn: () =>
      updateScreenerConfig({ ...(screenerSplit?.lists ?? {}), ...(screenerEdited ?? screenerData!) }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['screener-config'] })
      addToast('Config guardada', 'success')
      setScreenerEdited(null)
    },
    onError: (e: Error) => addToast(e.message || 'Error al guardar config', 'error'),
  })

  const isSaving = saveRules.isPending || saveScreener.isPending
  const currentDirty = tab === 'rules' ? !!rulesEdited : !!screenerEdited

  const handleSave = () => {
    if (tab === 'rules') saveRules.mutate()
    else saveScreener.mutate()
  }

  const handleClose = () => {
    const isDirty = !!rulesEdited || !!screenerEdited
    if (isDirty && !window.confirm('¿Cerrar sin guardar? Los cambios se perderán.')) return
    setRulesEdited(null)
    setScreenerEdited(null)
    onClose()
  }

  useEffect(() => { setJsonError(null) }, [tab, mode])

  // Stable key: changes only when server data changes (forces JsonEditor remount/reset)
  const rulesEditorKey = rulesSplit ? JSON.stringify(rulesSplit.rest) : 'empty'
  const screenerEditorKey = screenerSplit ? JSON.stringify(screenerSplit.rest) : 'empty'

  const activeSplit = tab === 'rules' ? rulesSplit : screenerSplit
  const activeEdited = tab === 'rules' ? rulesEdited : screenerEdited
  const setActiveEdited = tab === 'rules' ? setRulesEdited : setScreenerEdited
  // AutoForm/JsonEditor operan sobre "rest" (edits previos si hay, si no el valor del server)
  const restValue = (activeEdited ?? activeSplit?.rest ?? {}) as Record<string, unknown>

  const applyRestChange = (rest: Record<string, unknown>) => {
    setActiveEdited({ ...(activeSplit?.lists ?? {}), ...rest })
    setJsonError(null)
  }

  return (
    <Modal open={open} onClose={handleClose} title="Configuración" width="max-w-3xl">
      <div className="flex gap-1 mb-3 bg-surface-2 rounded-lg p-1">
        {(['rules', 'screener'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`flex-1 py-1.5 rounded-md text-sm font-medium transition-colors ${
              tab === t ? 'bg-surface-3 text-gray-100' : 'text-gray-500 hover:text-gray-300'
            }`}
          >
            {t === 'rules' ? 'Reglas de trading' : 'Screener / Estrategia'}
          </button>
        ))}
      </div>

      {/* Toggle Formulario | JSON avanzado */}
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-gray-500">
          {tab === 'rules'
            ? 'Parámetros de riesgo y ejecución. Los cambios se aplican en el próximo ciclo.'
            : 'Configuración de la estrategia activa. Algunos cambios requieren reinicio.'}
        </p>
        <div className="flex gap-1 bg-surface-2 rounded-lg p-1 shrink-0 ml-2">
          <button
            onClick={() => setMode('form')}
            title="Formulario"
            className={`p-1.5 rounded-md transition-colors ${mode === 'form' ? 'bg-surface-3 text-gray-100' : 'text-gray-500 hover:text-gray-300'}`}
          >
            <SlidersHorizontal size={14} />
          </button>
          <button
            onClick={() => setMode('json')}
            title="JSON avanzado"
            className={`p-1.5 rounded-md transition-colors ${mode === 'json' ? 'bg-surface-3 text-gray-100' : 'text-gray-500 hover:text-gray-300'}`}
          >
            <Code2 size={14} />
          </button>
        </div>
      </div>

      {activeSplit && (
        <div className="space-y-3">
          {mode === 'form' ? (
            <AutoForm value={restValue} onChange={applyRestChange} />
          ) : (
            <JsonEditor
              key={tab === 'rules' ? rulesEditorKey : screenerEditorKey}
              initialValue={restValue}
              onChange={applyRestChange}
              onError={setJsonError}
            />
          )}
          {Object.entries(activeSplit.lists).map(([k, v]) => (
            <SymbolListSection key={k} label={k} symbols={v} />
          ))}
        </div>
      )}

      <div className="flex items-center justify-between mt-4">
        <p className={`text-xs ${currentDirty ? 'text-yellow-400' : 'text-gray-500'}`}>
          {currentDirty ? '● Cambios sin guardar' : 'Sin cambios'}
        </p>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={handleClose}>Cerrar</Button>
          <Button
            variant="primary"
            onClick={handleSave}
            disabled={!currentDirty || !!jsonError || isSaving}
          >
            {isSaving ? 'Guardando…' : 'Guardar'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
