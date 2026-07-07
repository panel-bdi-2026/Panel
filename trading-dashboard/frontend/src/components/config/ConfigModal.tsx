import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchRules, updateRules, fetchScreenerConfig, updateScreenerConfig } from '../../api/config'
import { Modal } from '../ui/Modal'
import { Button } from '../ui/Button'
import { useToastStore } from '../ui/Toast'

type Tab = 'rules' | 'screener'

interface JsonEditorProps {
  value: Record<string, unknown>
  onChange: (v: Record<string, unknown>) => void
  onError: (e: string | null) => void
}

function JsonEditor({ value, onChange, onError }: JsonEditorProps) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2))
  const [localError, setLocalError] = useState<string | null>(null)

  useEffect(() => {
    setText(JSON.stringify(value, null, 2))
  }, [value])

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
    <div className="relative">
      <textarea
        value={text}
        onChange={(e) => handleChange(e.target.value)}
        className="w-full h-96 bg-gray-950 border border-gray-700 rounded-lg p-3 text-xs font-mono text-gray-300 focus:outline-none focus:border-brand-500 resize-none"
        spellCheck={false}
      />
      {localError && (
        <p className="text-red-400 text-xs mt-1">{localError}</p>
      )}
    </div>
  )
}

interface Props { open: boolean; onClose: () => void }

export function ConfigModal({ open, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('rules')
  const [rulesEdited, setRulesEdited] = useState<Record<string, unknown> | null>(null)
  const [screenerEdited, setScreenerEdited] = useState<Record<string, unknown> | null>(null)
  const [jsonError, setJsonError] = useState<string | null>(null)
  const qc = useQueryClient()
  const addToast = useToastStore((s) => s.add)

  const { data: rulesData } = useQuery({ queryKey: ['rules'], queryFn: fetchRules, enabled: open })
  const { data: screenerData } = useQuery({ queryKey: ['screener-config'], queryFn: fetchScreenerConfig, enabled: open })

  const saveRules = useMutation({
    mutationFn: () => updateRules(rulesEdited ?? rulesData!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['rules'] })
      addToast('Rules guardadas', 'success')
      setRulesEdited(null)
    },
    onError: (e: Error) => addToast(e.message || 'Error al guardar rules', 'error'),
  })

  const saveScreener = useMutation({
    mutationFn: () => updateScreenerConfig(screenerEdited ?? screenerData!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['screener-config'] })
      addToast('Config guardada', 'success')
      setScreenerEdited(null)
    },
    onError: (e: Error) => addToast(e.message || 'Error al guardar config', 'error'),
  })

  const isDirty = (!!rulesEdited) || (!!screenerEdited)
  const isSaving = saveRules.isPending || saveScreener.isPending

  const handleSave = () => {
    if (tab === 'rules') saveRules.mutate()
    else saveScreener.mutate()
  }

  const handleClose = () => {
    if (isDirty && !window.confirm('¿Cerrar sin guardar? Los cambios se perderán.')) return
    setRulesEdited(null)
    setScreenerEdited(null)
    onClose()
  }

  return (
    <Modal open={open} onClose={handleClose} title="Configuración" width="max-w-3xl">
      {/* Tabs */}
      <div className="flex gap-1 mb-4 bg-gray-800 rounded-lg p-1">
        {(['rules', 'screener'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => { setTab(t); setJsonError(null) }}
            className={`flex-1 py-1.5 rounded-md text-sm font-medium transition-colors ${
              tab === t ? 'bg-gray-700 text-gray-100' : 'text-gray-500 hover:text-gray-300'
            }`}
          >
            {t === 'rules' ? 'Reglas de trading' : 'Screener / Universo'}
          </button>
        ))}
      </div>

      {tab === 'rules' && rulesData && (
        <JsonEditor
          value={rulesEdited ?? rulesData}
          onChange={setRulesEdited}
          onError={setJsonError}
        />
      )}
      {tab === 'screener' && screenerData && (
        <JsonEditor
          value={screenerEdited ?? screenerData}
          onChange={setScreenerEdited}
          onError={setJsonError}
        />
      )}

      <div className="flex items-center justify-between mt-4">
        <p className={`text-xs ${isDirty ? 'text-yellow-400' : 'text-gray-500'}`}>
          {isDirty ? '● Cambios sin guardar' : 'Sin cambios'}
        </p>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={handleClose}>Cerrar</Button>
          <Button
            variant="primary"
            onClick={handleSave}
            disabled={!isDirty || !!jsonError || isSaving}
          >
            {isSaving ? 'Guardando…' : 'Guardar'}
          </Button>
        </div>
      </div>
    </Modal>
  )
}
