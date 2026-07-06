export type Section =
  | 'funds'
  | 'signals'
  | 'orders'
  | 'positions'
  | 'audit'
  | 'backtest'
  | 'config'

interface NavItem { id: Section; label: string; icon: string }

const NAV: NavItem[] = [
  { id: 'funds',     label: 'Fondos',     icon: '💼' },
  { id: 'signals',   label: 'Señales',    icon: '📡' },
  { id: 'orders',    label: 'Órdenes',    icon: '📋' },
  { id: 'positions', label: 'Posiciones', icon: '📊' },
  { id: 'audit',     label: 'Auditoría',  icon: '📜' },
  { id: 'backtest',  label: 'Backtest',   icon: '🧪' },
  { id: 'config',    label: 'Config',     icon: '⚙️' },
]

interface Props {
  active: Section
  onSelect: (s: Section) => void
  pendingOrders?: number
  collapsed: boolean
  onToggle: () => void
}

export function Sidebar({ active, onSelect, pendingOrders = 0, collapsed, onToggle }: Props) {
  return (
    <aside className={`flex flex-col bg-gray-900 border-r border-gray-800 transition-all duration-200 ${collapsed ? 'w-14' : 'w-52'} shrink-0`}>
      <div className="flex items-center justify-between px-3 py-4 border-b border-gray-800">
        {!collapsed && (
          <span className="text-sm font-bold text-gray-200 tracking-wide">Trading Panel</span>
        )}
        <button onClick={onToggle} className="text-gray-500 hover:text-gray-300 p-1 rounded ml-auto">
          {collapsed ? '▶' : '◀'}
        </button>
      </div>

      <nav className="flex-1 py-2">
        {NAV.map((item) => {
          const isActive = active === item.id
          return (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              className={`w-full flex items-center gap-3 px-3 py-2.5 text-sm transition-colors ${
                isActive
                  ? 'bg-brand-600/20 text-brand-400 border-r-2 border-brand-500'
                  : 'text-gray-400 hover:text-gray-200 hover:bg-gray-800'
              }`}
            >
              <span className="text-base shrink-0">{item.icon}</span>
              {!collapsed && (
                <span className="truncate">{item.label}</span>
              )}
              {!collapsed && item.id === 'orders' && pendingOrders > 0 && (
                <span className="ml-auto bg-yellow-500 text-black text-xs font-bold px-1.5 py-0.5 rounded-full">
                  {pendingOrders}
                </span>
              )}
            </button>
          )
        })}
      </nav>
    </aside>
  )
}
