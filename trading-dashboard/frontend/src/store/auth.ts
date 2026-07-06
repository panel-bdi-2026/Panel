import { create } from 'zustand'
import { persist } from 'zustand/middleware'

interface AuthState {
  apiKey: string | null
  setApiKey: (key: string) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      apiKey: null,
      setApiKey: (key) => set({ apiKey: key }),
      logout: () => set({ apiKey: null }),
    }),
    { name: 'panel-auth' },
  ),
)
