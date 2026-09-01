import { useState, useEffect } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useAuthStore } from './store/auth'
import { Shell } from './components/layout/Shell'

const qc = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      staleTime: 5000,
    },
  },
})

function LoginOverlay({ onLogin }: { onLogin: () => void }) {
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!key.trim()) return
    setLoading(true)
    setError('')
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: key.trim() }),
      })
      if (!res.ok) {
        setError('Contraseña inválida')
        return
      }
      onLogin()
    } catch {
      setError('No se pudo conectar con el servidor')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-950 flex items-center justify-center">
      <div className="bg-gray-900 border border-gray-800 rounded-2xl p-8 w-full max-w-sm shadow-2xl">
        <h1 className="text-xl font-bold text-gray-100 mb-1">Panel Trading</h1>
        <p className="text-sm text-gray-500 mb-6">Ingresá tu contraseña para continuar</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Contraseña</label>
            <input
              type="password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="••••••••"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-brand-500"
              autoFocus
            />
          </div>
          {error && <p className="text-xs text-red-400">{error}</p>}
          <button
            type="submit"
            disabled={loading || !key.trim()}
            className="w-full bg-brand-600 hover:bg-brand-500 disabled:opacity-50 text-white font-semibold rounded-lg py-2 text-sm transition-colors"
          >
            {loading ? 'Verificando…' : 'Entrar'}
          </button>
        </form>
      </div>
    </div>
  )
}

export default function App() {
  const { authed, setAuthed } = useAuthStore()
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    // Limpiar la API key que la versión anterior guardaba en localStorage
    localStorage.removeItem('panel-auth')

    fetch('/api/status')
      .then((r) => {
        setAuthed(r.ok)
        setChecking(false)
      })
      .catch(() => {
        setAuthed(false)
        setChecking(false)
      })
  }, [])

  if (checking) return null

  if (!authed) {
    return <LoginOverlay onLogin={() => setAuthed(true)} />
  }

  return (
    <QueryClientProvider client={qc}>
      <Shell />
    </QueryClientProvider>
  )
}
