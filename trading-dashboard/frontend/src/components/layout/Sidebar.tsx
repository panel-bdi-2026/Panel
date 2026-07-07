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
  Menu,
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
  { id: 'signals',   label: 'Señales',    Icon: Radio },
  { id: 'orders',    label: 'Órdenes',    Icon: ClipboardList },
  { id: 'positions', label: 'Posiciones', Icon: BarChart2 },
  { id: 'audit',     label: 'Auditoría',  Icon: ScrollText },
  { id: 'backtest',  label: 'Backtest',   Icon: FlaskConical },
  { id: 'config',    label: 'Config',     Icon: Settings },
]

interface Props {
  active: Section
  onSelect: (s: Section) => void
  pendingOrders?: number
  collapsed: boolean
  onToggle: () => void
}

function NavContent({
  active, onSelect, pendingOrders, collapsed, onToggle,
}: Required<Props>) {
  return (
    <>
      <div className={`flex items-center border-b border-gray-800 h-14 px-3 ${collapsed ? 'justify-center' : 'justify-between'}`}>
        {!collapsed && (
          <span className="text-xs font-semibold text-gray-400 tracking-widest uppercase">Panel</span>
        )}
        <button
          onClick={onToggle}
          className="text-gray-500 hover:text-gray-300 p-1.5 rounded-lg hover:bg-gray-800 transition-colors"
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
                  : 'text-gray-500 hover:text-gray-200 hover:bg-gray-800/60'
              }`}
            >
              <item.Icon size={18} className="shrink-0" />
              {!collapsed && (
                <span className="truncate font-medium">{item.label}</span>
              )}
              {item.id === 'orders' && pendingOrders > 0 && (
                <span className={`${collapsed ? 'absolute top-1.5 right-1.5' : 'ml-auto'} bg-yellow-500 text-black text-[10px] font-bold px-1.5 py-0.5 rounded-full min-w-[18px] text-center`}>
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
  function handleSelectMobile(s: Section) {
    onSelect(s)
    if (!collapsed) onToggle()
  }

  return (
    <>
      {/* Desktop: static sidebar */}
      <aside className={`hidden sm:flex flex-col bg-gray-900 border-r border-gray-800 transition-all duration-200 ${collapsed ? 'w-14' : 'w-48'} shrink-0`}>
        <NavContent
          active={active}
          onSelect={onSelect}
          pendingOrders={pendingOrders}
          collapsed={collapsed}
          onToggle={onToggle}
        />
      </aside>

      {/* Mobile: icon-only strip always visible */}
      <aside className="sm:hidden flex flex-col bg-gray-900 border-r border-gray-800 w-12 shrink-0">
        <div className="flex items-center justify-center h-14 border-b border-gray-800">
          <button
            onClick={onToggle}
            className="text-gray-500 hover:text-gray-300 p-1.5 rounded-lg hover:bg-gray-800 transition-colors"
          >
            <Menu size={18} />
          </button>
        </div>
        <nav className="flex-1 py-2 space-y-0.5">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              title={item.label}
              className={`w-full flex items-center justify-center py-3 transition-colors relative ${
                active === item.id ? 'text-brand-400' : 'text-gray-600 hover:text-gray-300'
              }`}
            >
              <item.Icon size={18} />
              {item.id === 'orders' && pendingOrders > 0 && (
                <span className="absolute top-1.5 right-1.5 w-3.5 h-3.5 bg-yellow-500 rounded-full text-[8px] font-bold text-black flex items-center justify-center">
                  {pendingOrders > 9 ? '9+' : pendingOrders}
                </span>
              )}
            </button>
          ))}
        </nav>
      </aside>

      {/* Mobile: full drawer overlay when expanded */}
      {!collapsed && (
        <>
          <div
            className="sm:hidden fixed inset-0 z-40 bg-black/50 backdrop-blur-sm"
            onClick={onToggle}
          />
          <aside className="sm:hidden fixed left-0 top-0 bottom-0 z-50 w-52 flex flex-col bg-gray-900 border-r border-gray-800 shadow-2xl">
            <NavContent
              active={active}
              onSelect={handleSelectMobile}
              pendingOrders={pendingOrders}
              collapsed={false}
              onToggle={onToggle}
            />
          </aside>
        </>
      )}
    </>
  )
}
