import { create } from 'zustand'

interface AuthState {
  authed: boolean
  setAuthed: (v: boolean) => void
  logout: () => void
}

export const useAuthStore = create<AuthState>()((set) => ({
  authed: false,
  setAuthed: (v) => set({ authed: v }),
  logout: () => {
    fetch('/api/logout', { method: 'POST' }).catch(() => {})
    set({ authed: false })
  },
}))
