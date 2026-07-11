import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Header } from './Header'
import { Sidebar, type Section } from './Sidebar'
import { fetchPendingOrders } from '../../api/orders'
import { useWebSocket } from '../../hooks/useWebSocket'
import { useNotifications } from '../../hooks/useNotifications'
import { ToastContainer } from '../ui/Toast'
import { AccountSummaryPanel } from '../account/AccountSummary'
import { FundList } from '../funds/FundList'
import { EquityChart } from '../funds/EquityChart'
import { SignalsTable } from '../signals/SignalsTable'
import { PendingOrders } from '../orders/PendingOrders'
import { NewOrderForm } from '../orders/NewOrderForm'
import { PositionsTable } from '../positions/PositionsTable'
import { AuditTimeline } from '../audit/AuditTimeline'
import { BacktestPanel } from '../backtest/BacktestPanel'
import { ConfigModal } from '../config/ConfigModal'
import { ControlBar } from '../system/ControlBar'
import { HealthBanner } from '../system/HealthBanner'
import { Button } from '../ui/Button'

export function Shell() {
  useWebSocket()
  useNotifications()

  const [section, setSection] = useState<Section>('funds')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(true)
  const [showNewOrder, setShowNewOrder] = useState(false)
  const [newOrderSymbol, setNewOrderSymbol] = useState<string | undefined>()
  const [showConfig, setShowConfig] = useState(false)

  const { data: orders } = useQuery({
    queryKey: ['orders', 'pending'],
    queryFn: fetchPendingOrders,
    refetchInterval: 10000,
  })

  const pendingCount = orders?.filter((o) => o.status === 'pending').length ?? 0

  function openOrder(symbol?: string) {
    setNewOrderSymbol(symbol)
    setShowNewOrder(true)
  }

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
        <Header
          onConfig={() => setShowConfig(true)}
        />
        <HealthBanner />
        <ControlBar onNewOrder={() => openOrder()} />
        <AccountSummaryPanel />
        <main className="flex-1 overflow-auto p-3 sm:p-4 pb-20 sm:pb-4 space-y-4">
          {section !== 'funds' && (
            <h2 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
              {section === 'signals'   && 'Señales'}
              {section === 'orders'    && 'Órdenes'}
              {section === 'positions' && 'Posiciones'}
              {section === 'audit'     && 'Auditoría'}
              {section === 'backtest'  && 'Backtest'}
              {section === 'config'    && 'Configuración'}
            </h2>
          )}
          {section === 'funds' && (
            <>
              <EquityChart />
              <FundList />
            </>
          )}
          {section === 'signals' && (
            <SignalsTable onOrder={(sym) => openOrder(sym)} />
          )}
          {section === 'orders'    && <PendingOrders />}
          {section === 'positions' && <PositionsTable />}
          {section === 'audit'     && <AuditTimeline />}
          {section === 'backtest'  && <BacktestPanel />}
          {section === 'config'    && (
            <div className="flex flex-col items-center justify-center py-12 gap-4">
              <p className="text-gray-500 text-sm">Editá la configuración del sistema desde el modal</p>
              <Button variant="primary" onClick={() => setShowConfig(true)}>
                Abrir configuración
              </Button>
            </div>
          )}
        </main>
      </div>

      <NewOrderForm
        open={showNewOrder}
        initialSymbol={newOrderSymbol}
        onClose={() => { setShowNewOrder(false); setNewOrderSymbol(undefined) }}
      />
      <ConfigModal open={showConfig} onClose={() => setShowConfig(false)} />
      <ToastContainer />
    </div>
  )
}
