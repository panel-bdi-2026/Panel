import { useState } from 'react'
import {
  Briefcase,
  Radio,
  ClipboardList,
  BarChart2,
  ScrollText,
  FlaskConical,
  Settings,
  ChevronLeft,
  ChevronRight,
  MoreHorizontal,
  X,
  type LucideIcon,
} from 'lucide-react'

export type Section =
  | 'funds'
  | 'signals'
  | 'orders'
  | 'positions'
  | 'audit'
  | 'backtest'
  | 'config'

interface NavItem { id: Section; label: string; Icon: LucideIcon }

const NAV: NavItem[] = [
  { id: 'funds',     label: 'Fondos',     Icon: Briefcase },
  { id: 'signals',   label: 'Radar',      Icon: Radio },
  { id: 'orders',    label: 'Órdenes',    Icon: ClipboardList },
  { id: 'positions', label: 'Posiciones', Icon: BarChart2 },
  { id: 'audit',     label: 'Auditoría',  Icon: ScrollText },
  { id: 'backtest',  label: 'Backtest',   Icon: FlaskConical },
  { id: 'config',    label: 'Config',     Icon: Settings },
]

// Primeras 4 en la bottom bar; el resto va bajo "Más".
const BOTTOM_PRIMARY: Section[] = ['funds', 'signals', 'orders', 'positions']
const MORE_SECTIONS: Section[] = ['audit', 'backtest', 'config']

interface Props {
  active: Section
  onSelect: (s: Section) => void
  pendingOrders?: number
  collapsed: boolean
  onToggle: () => void
}

// ── Desktop sidebar ──
function NavContent({ active, onSelect, pendingOrders, collapsed, onToggle }: Required<Props>) {
  return (
    <>
      <div className={`flex items-center border-b border-surface-3 h-14 px-3 ${collapsed ? 'justify-center' : 'justify-between'}`}>
        {!collapsed && (
          <span className="text-xs font-semibold text-gray-400 tracking-widest uppercase">Panel</span>
        )}
        <button
          onClick={onToggle}
          className="text-gray-500 hover:text-gray-300 p-1.5 rounded-lg hover:bg-surface-2 transition-colors"
        >
          {collapsed ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
        </button>
      </div>
      <nav className="flex-1 py-2 space-y-0.5">
        {NAV.map((item) => {
          const isActive = active === item.id
          return (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              title={collapsed ? item.label : undefined}
              className={`w-full flex items-center gap-3 px-3 py-2.5 text-sm transition-colors relative ${
                isActive
                  ? 'bg-brand-600/15 text-brand-400 border-r-2 border-brand-500'
                  : 'text-gray-500 hover:text-gray-200 hover:bg-surface-2/60'
              }`}
            >
              <item.Icon size={18} className="shrink-0" />
              {!collapsed && <span className="truncate font-medium">{item.label}</span>}
              {item.id === 'orders' && pendingOrders > 0 && (
                <span className={`${collapsed ? 'absolute top-1.5 right-1.5' : 'ml-auto'} bg-warn text-black text-[10px] font-bold px-1.5 py-0.5 rounded-full min-w-[18px] text-center`}>
                  {pendingOrders}
                </span>
              )}
            </button>
          )
        })}
      </nav>
    </>
  )
}

export function Sidebar({ active, onSelect, pendingOrders = 0, collapsed, onToggle }: Props) {
  const [moreOpen, setMoreOpen] = useState(false)
  const moreActive = MORE_SECTIONS.includes(active)

  function pick(s: Section) {
    onSelect(s)
    setMoreOpen(false)
  }

  return (
    <>
      {/* Desktop: sidebar estático */}
      <aside className={`hidden sm:flex flex-col bg-surface-1 border-r border-surface-3 transition-all duration-200 ${collapsed ? 'w-14' : 'w-48'} shrink-0`}>
        <NavContent
          active={active}
          onSelect={onSelect}
          pendingOrders={pendingOrders}
          collapsed={collapsed}
          onToggle={onToggle}
        />
      </aside>

      {/* Móvil: bottom tab bar (patrón IBKR GlobalTrader) */}
      <nav className="sm:hidden fixed bottom-0 inset-x-0 z-40 flex items-stretch bg-surface-1 border-t border-surface-3 pb-[env(safe-area-inset-bottom)]">
        {BOTTOM_PRIMARY.map((id) => {
          const item = NAV.find((n) => n.id === id)!
          const isActive = active === id
          return (
            <button
              key={id}
              onClick={() => onSelect(id)}
              className={`flex-1 flex flex-col items-center justify-center gap-0.5 py-2 relative ${
                isActive ? 'text-brand-400' : 'text-gray-500'
              }`}
            >
              <item.Icon size={20} />
              <span className="text-[10px] font-medium">{item.label}</span>
              {id === 'orders' && pendingOrders > 0 && (
                <span className="absolute top-1 right-1/2 translate-x-4 w-4 h-4 bg-warn rounded-full text-[9px] font-bold text-black flex items-center justify-center">
                  {pendingOrders > 9 ? '9+' : pendingOrders}
                </span>
              )}
            </button>
          )
        })}
        {/* Más */}
        <button
          onClick={() => setMoreOpen(true)}
          className={`flex-1 flex flex-col items-center justify-center gap-0.5 py-2 ${moreActive ? 'text-brand-400' : 'text-gray-500'}`}
        >
          <MoreHorizontal size={20} />
          <span className="text-[10px] font-medium">Más</span>
        </button>
      </nav>

      {/* Sheet "Más" */}
      {moreOpen && (
        <div className="sm:hidden fixed inset-0 z-50 flex flex-col justify-end">
          <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={() => setMoreOpen(false)} />
          <div className="relative bg-surface-1 border-t border-surface-3 rounded-t-xl2 pb-[env(safe-area-inset-bottom)]">
            <div className="flex items-center justify-between px-4 py-3 border-b border-surface-3">
              <span className="text-sm font-semibold text-gray-200">Más</span>
              <button onClick={() => setMoreOpen(false)} className="text-gray-500 p-1"><X size={18} /></button>
            </div>
            <div className="py-2">
              {MORE_SECTIONS.map((id) => {
                const item = NAV.find((n) => n.id === id)!
                const isActive = active === id
                return (
                  <button
                    key={id}
                    onClick={() => pick(id)}
                    className={`w-full flex items-center gap-3 px-4 py-3.5 text-sm ${
                      isActive ? 'text-brand-400 bg-brand-600/10' : 'text-gray-300'
                    }`}
                  >
                    <item.Icon size={18} />
                    <span className="font-medium">{item.label}</span>
                  </button>
                )
              })}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
