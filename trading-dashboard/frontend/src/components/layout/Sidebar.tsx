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

function NavContent({
  active, onSelect, pendingOrders, collapsed, onToggle,
}: Required<Props>) {
  return (
    <>
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
    </>
  )
}

export function Sidebar({ active, onSelect, pendingOrders = 0, collapsed, onToggle }: Props) {
  function handleSelectMobile(s: Section) {
    onSelect(s)
    onToggle()
  }

  return (
    <>
      {/* Desktop: static sidebar */}
      <aside className={`hidden sm:flex flex-col bg-gray-900 border-r border-gray-800 transition-all duration-200 ${collapsed ? 'w-14' : 'w-52'} shrink-0`}>
        <NavContent
          active={active}
          onSelect={onSelect}
          pendingOrders={pendingOrders}
          collapsed={collapsed}
          onToggle={onToggle}
        />
      </aside>

      {/* Mobile: icon-only strip always visible */}
      <aside className="sm:hidden flex flex-col bg-gray-900 border-r border-gray-800 w-14 shrink-0">
        <div className="flex items-center justify-center py-4 border-b border-gray-800">
          <button onClick={onToggle} className="text-gray-500 hover:text-gray-300 p-1 rounded">
            ☰
          </button>
        </div>
        <nav className="flex-1 py-2">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => onSelect(item.id)}
              className={`w-full flex items-center justify-center py-2.5 transition-colors relative ${
                active === item.id ? 'text-brand-400' : 'text-gray-500 hover:text-gray-300'
              }`}
            >
              <span className="text-base">{item.icon}</span>
              {item.id === 'orders' && pendingOrders > 0 && (
                <span className="absolute top-1.5 right-1.5 w-3 h-3 bg-yellow-500 rounded-full text-[8px] font-bold text-black flex items-center justify-center">
                  {pendingOrders}
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
            className="sm:hidden fixed inset-0 z-40 bg-black/50"
            onClick={onToggle}
          />
          <aside className="sm:hidden fixed left-0 top-0 bottom-0 z-50 w-52 flex flex-col bg-gray-900 border-r border-gray-800">
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
