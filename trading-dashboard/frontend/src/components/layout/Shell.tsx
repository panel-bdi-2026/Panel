import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Header } from './Header'
import { Sidebar, type Section } from './Sidebar'
import { fetchPendingOrders } from '../../api/orders'
import { useWebSocket } from '../../hooks/useWebSocket'
import { ToastContainer } from '../ui/Toast'
import { AccountSummaryPanel } from '../account/AccountSummary'
import { FundList } from '../funds/FundList'
import { SignalsTable } from '../signals/SignalsTable'
import { PendingOrders } from '../orders/PendingOrders'
import { PositionsTable } from '../positions/PositionsTable'
import { AuditTimeline } from '../audit/AuditTimeline'

export function Shell() {
  useWebSocket()
  const [section, setSection] = useState<Section>('funds')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  const { data: orders } = useQuery({
    queryKey: ['orders', 'pending'],
    queryFn: fetchPendingOrders,
    refetchInterval: 10000,
  })

  const pendingCount = orders?.filter((o) => o.status === 'pending').length ?? 0

  return (
    <div className="flex h-screen overflow-hidden bg-gray-950">
      <Sidebar
        active={section}
        onSelect={setSection}
        pendingOrders={pendingCount}
        collapsed={sidebarCollapsed}
        onToggle={() => setSidebarCollapsed((v) => !v)}
      />

      <div className="flex flex-col flex-1 min-w-0">
        <Header />
        <AccountSummaryPanel />
        <main className="flex-1 overflow-auto p-4">
          {section === 'funds'     && <FundList />}
          {section === 'signals'   && <SignalsTable />}
          {section === 'orders'    && <PendingOrders />}
          {section === 'positions' && <PositionsTable />}
          {section === 'audit'     && <AuditTimeline />}
          {section === 'backtest'  && <div className="text-gray-500 text-sm">Backtest — próximamente</div>}
          {section === 'config'    && <div className="text-gray-500 text-sm">Config — próximamente</div>}
        </main>
      </div>

      <ToastContainer />
    </div>
  )
}
