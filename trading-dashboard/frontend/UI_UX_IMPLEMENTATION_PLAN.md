# Plan de Implementación UI/UX — Trading Dashboard (para ejecutar con Sonnet)

> Documento de ejecución paso a paso. Objetivo: llevar el dashboard a calidad
> "app IBKR GlobalTrader" **sin perder ninguna funcionalidad existente**.
> Ejecutar fase por fase, en orden. Cada fase deja el panel usable y deployable.

---

## 0. REGLAS DE EJECUCIÓN (LEER PRIMERO)

### Stack (no cambiar)
React 19 · Vite · TypeScript · TailwindCSS 3 · TanStack Query 5 · Zustand 5 ·
Recharts 3 · lucide-react. Todo el trabajo es incremental sobre este stack.

### Rutas
- Frontend: `/opt/panel/trading-dashboard/frontend/` (código en `src/`)
- Backend: `/opt/panel/trading-dashboard/backend/app/main.py`
- El backend sirve el build desde `frontend/dist` vía StaticFiles
  (`FRONTEND_DIR` en main.py:55).

### Deploy de cada fase
```bash
cd /opt/panel/trading-dashboard/frontend
npm run build          # tsc -b && vite build → regenera dist/
```
- `dist/` se sirve solo (StaticFiles lee de disco); no hace falta reiniciar el
  backend salvo que se toque `main.py` → ahí sí:
  `sudo systemctl restart trading-dashboard`.
- Verificar en **móvil real** (no solo devtools), hard refresh.
- Si `npm run build` falla por TypeScript, corregir antes de seguir. No deployar
  con errores de tipo.

### Idioma
Todo el texto de UI en **español** (es la lengua del proyecto).

### Git / ramas
Rama de trabajo del proyecto: `claude/investment-dashboard-trades-x2vkn8`.
Commitear al final de cada fase con mensaje descriptivo. No pushear a otra rama.

---

## 1. CONTRATO DE PRESERVACIÓN (features que NO se pueden romper)

> Cada rediseño debe mantener TODA esta funcionalidad. Antes de dar una fase por
> terminada, verificar que cada ítem sigue funcionando.

### 1.1 Administración por FONDOS (feature central y diferencial)
- Lista de fondos (`FundList.tsx`) con: nombre, estrategia, cash disponible,
  capital aportado, P&L realizado, ROI, nº de posiciones, badges AUTO/CERRADO.
- Detalle de fondo (`FundDetailModal.tsx`): posiciones, trades, flujos de
  capital, toggle auto-trading, aportar/retirar capital, cerrar fondo.
- Endpoints: `GET /api/funds`, `GET /api/funds/:id`, `POST /api/funds`,
  `POST /api/funds/:id/close`, `POST /api/funds/:id/capital-flows`,
  `POST /api/funds/:id/auto-trading`, `GET /api/funds/roi-history`.
- Gráfico de equity/ROI por fondo (`EquityChart.tsx`, usa `fetchRoiHistory`).
- **Concepto clave**: cada fondo es una cartera virtual independiente con su
  propio capital, estrategia y auto-trading. NO es una cuenta IBKR separada.
  Esta separación por fondos es el corazón del producto — preservarla intacta.

### 1.2 RADAR / Señales (`SignalsTable.tsx`, "el radar")
- Tabla de señales con score por estrategia (opportunistic, momentum,
  long_term, dividend), barras de score con color por umbral (≥75 verde, ≥50
  azul, ≥30 amarillo).
- Precios live cada 5s (`fetchLivePrices`), panel de detalle lateral por símbolo,
  botón "+ Orden" que abre el form con el símbolo precargado.
- Endpoints: `GET /api/signals/scan/all`, `GET /api/signals/live-prices`.
- Estado de carga (skeleton) y vacío ("Sin señales activas 📡") YA existen —
  preservar/mejorar, no eliminar.

### 1.3 BACKTEST (`BacktestPanel.tsx`)
- Modo Backtest y Walk-forward, selector de estrategia (solo las que
  `supports_backtest`), selector de folds (2-5).
- Métricas: retorno total, Sharpe, max drawdown, DSR (con semáforo), win rate,
  expectancy, profit factor, exposición, nº trades, avg win/loss, alpha.
- Curva de equity (Recharts), conteo de motivos de salida.
- Walk-forward: tabla de folds con retorno/bench/sharpe/DD/trades, nº positivos.
- Endpoints: `GET /api/signals/backtest`, `GET /api/signals/backtest/walk-forward`,
  `GET /api/strategies`. Puede tardar varios minutos — mantener el estado de
  "Calculando…".

### 1.4 Órdenes
- Pendientes (`PendingOrders.tsx`): aprobar/rechazar, violaciones de reglas,
  valor estimado. Endpoints `GET /api/orders/pending`, `POST /api/orders/:id/approve`,
  `POST /api/orders/:id/reject`.
- Nueva orden (`NewOrderForm.tsx`): symbol, side, qty, tipo, límite, stop-loss,
  fund_id; sugerencia de tamaño (`/api/orders/size-suggestion`). `POST /api/orders`.

### 1.5 Posiciones (`PositionsTable.tsx`)
- Symbol, qty, avg cost, market value, unrealized P&L (valor y %).
- `GET /api/positions`.

### 1.6 Auditoría (`AuditTimeline.tsx`)
- Timeline de eventos (`GET /api/audit?limit=`): acción, payload, resultado, ts.

### 1.7 Configuración (`ConfigModal.tsx`)
- Reglas (`/api/rules`) y config de señales (`/api/signals/config`).

### 1.8 Cuenta / Estado (`AccountSummary.tsx`, `Header.tsx`)
- Net Liq, Cash, P&L día, Buying Power. `GET /api/account`.
- Estado: connected, mode (paper/live), halted, last_scan_at, scan_stale.
  `GET /api/status`, `POST /api/mode`, `POST /api/halt`.

### 1.9 Realtime
- WebSocket (`useWebSocket.ts`, store `realtime.ts`), notificaciones
  (`useNotifications.ts`), toasts (`Toast.tsx`). Login por API key (`App.tsx`,
  store `auth.ts`).

---

## 2. FASE A — Controles críticos y confiabilidad (PRIORIDAD MÁXIMA)

> Motivación: incidente real (2026-07-11) — IBKR se desconectó, trading quedó
> halted, y desde el celular no se pudo recuperar. Además el toggle de halt
> está ROTO (ver A0).

### A0. 🐞 BUG CRÍTICO — arreglar el toggle de halt (empezar por acá)
**Problema**: `toggleHalt()` en `src/api/account.ts` hace
`POST /api/halt` sin el query param `value`, pero el backend
(`main.py` `set_halt(value: bool)`) lo exige → **422, el botón nunca funciona**.

**Fix backend** (`main.py`, endpoint `/api/halt`): aceptar toggle sin romper
compatibilidad. Cambiar la firma para que `value` sea opcional y, si no viene,
invierta el estado actual:
```python
@app.post("/api/halt")
def set_halt(value: bool | None = None, _: None = Depends(require_api_key)):
    state["halted"] = (not state["halted"]) if value is None else value
    _persist_state()
    audit.record("halt_toggle", {"value": state["halted"]}, {})
    return {"halted": state["halted"]}
```
**Fix frontend** (`src/api/account.ts`): pasar el valor explícito para que no
sea ambiguo (mejor que confiar en el toggle del server):
```ts
export const setHalt = (halted: boolean) =>
  apiFetch<{ halted: boolean }>(`/api/halt?value=${halted}`, { method: 'POST' })
```
Actualizar `Header.tsx` para llamar `setHalt(!status.halted)`.
**Verificar**: pausar y reanudar desde el UI realmente cambia el estado (ver
`/api/status`).

### A1. Endpoint + acción "Reconectar IBKR"
Hoy no hay forma de reconectar IBKR desde el UI (solo reiniciar backend por SSH
o togglear modo — peligroso). Agregar:

**Backend** (`main.py`): nuevo endpoint que reusa `broker.reconnect(...)` (ya
usado en startup y en `/api/mode`), sin tocar modo ni halt:
```python
@app.post("/api/reconnect")
async def reconnect_ibkr(_: None = Depends(require_api_key)):
    target_port = settings.ib_port_live if state["mode"] == "live" else settings.ib_port_paper
    try:
        await broker.reconnect(settings.ib_host, target_port, settings.ib_client_id)
        state["connected"] = True
        _persist_state()
        audit.record("ibkr_reconnect", {"mode": state["mode"]}, {"ok": True})
        return {"connected": True}
    except IBKRConnectionError as exc:
        state["connected"] = False
        audit.record("ibkr_reconnect", {"mode": state["mode"]}, {"ok": False, "error": str(exc)})
        raise HTTPException(status_code=503, detail=f"No se pudo reconectar: {exc}")
```
**Frontend** (`src/api/account.ts`): `export const reconnectIbkr = () =>
apiFetch<{connected:boolean}>('/api/reconnect', { method: 'POST' })`.

### A2. Health banner accionable (`GET /api/health`)
Crear `src/api/health.ts` (`fetchHealth`) y componente
`src/components/system/HealthBanner.tsx`. Cuando `status !== 'ok'`, renderizar
una barra arriba del `<main>` (dentro de `Shell.tsx`, debajo del Header) con los
issues activos y una acción por cada uno:
- `ibkr_disconnected` → botón "Reconectar IBKR" (A1).
- `trading_halted` → botón "Reanudar" (A0).
- `scan_stale` → texto "Último scan hace X" + link a estado.
Colores: 🟡 degradado, 🔴 crítico. Con `useMutation` + toasts + invalidar
`['status']` y `['health']`.

### A3. Barra de "quick actions" persistente (patrón pills de IBKR)
En `Header.tsx` (o un nuevo `ControlBar.tsx` bajo el header), fila de pills
redondeadas **siempre visibles también en móvil** (nada de `hidden sm:block`):
- `▶ Reanudar` / `⏸ Pausar` (según halted) — tap target ≥44px, con confirmación.
- `🔌 Reconectar` (mostrar destacado si `!connected`).
- `+ Nueva orden`.
- Indicador de estado 🟢/🟡/🔴 a la izquierda (derivado de health).
Confirmación clara al reanudar: "¿Reanudar trading en modo PAPER/LIVE?".

### A4. Feedback honesto post-acción
Tras reanudar, si IBKR sigue desconectado, el toast/health debe decirlo:
"Trading reanudado, pero IBKR sigue desconectado — reconectá para reanudar el
scan." No mostrar "OK" si el sistema sigue degradado.

**Aceptación Fase A**: desde el celular se puede pausar, reanudar y reconectar
IBKR; el banner de salud aparece cuando algo está mal y sus botones funcionan.

---

## 3. FASE B — Sistema de diseño (fundación visual)

> Sin esto, lo visual queda inconsistente. Tema **oscuro por defecto** (mejor
> para trading nocturno; IBKR es claro pero adoptamos su estructura, no su tema).

### B1. Tokens en `tailwind.config.js`
Extender el theme con superficies de elevación y color semántico. Reemplazar el
uso disperso de `gray-950/900/800` por tokens nombrados:
```js
extend: {
  colors: {
    brand: { /* mantener escala actual */ },
    surface: {
      0: '#0a0b0d',   // fondo app
      1: '#121316',   // card
      2: '#1a1c20',   // card elevada / hover
      3: '#232629',   // overlay / borde activo
    },
    profit: { DEFAULT: '#22c55e', dim: '#16a34a', bg: 'rgba(34,197,94,0.10)' },
    loss:   { DEFAULT: '#ef4444', dim: '#dc2626', bg: 'rgba(239,68,68,0.10)' },
    warn:   { DEFAULT: '#f59e0b' },
  },
  boxShadow: {
    card: '0 1px 3px rgba(0,0,0,0.4)',
    elevated: '0 8px 30px rgba(0,0,0,0.5)',
  },
  borderRadius: { xl2: '1rem' },
}
```
Migración gradual: no hace falta reemplazar todo de una; usar los tokens nuevos
en cada componente que se toque en fases siguientes.

### B2. Tipografía financiera (`index.css`)
- Cargar fuente Inter (o Geist) vía `@import` o link en `index.html`; aplicar a
  `body`. Fallback a system-ui.
- Utilidad `.nums { font-variant-numeric: tabular-nums; }` y aplicarla a TODA
  cifra monetaria/porcentual (hoy solo en SignalsTable). Evita el "baile" de
  números al actualizarse.
- Jerarquía: definir clases de composición o convención — hero (`text-4xl
  font-bold`), label (`text-xs uppercase tracking-wide text-gray-500`).

### B3. Primitivos UI nuevos (en `src/components/ui/`)
Crear (reutilizables en todas las fases):
- `Card.tsx` — contenedor con `bg-surface-1 border border-surface-3 rounded-xl2
  shadow-card`, variantes de padding.
- `Stat.tsx` — label + valor, con prop `delta` opcional (número + flecha + color
  profit/loss) y `mono`.
- `Sparkline.tsx` — mini gráfico de línea (Recharts o SVG puro) para series
  cortas; props `data:number[]`, `color`.
- `Skeleton.tsx` — bloque con `animate-pulse bg-surface-2 rounded`.
- `EmptyState.tsx` — ícono + título + subtítulo (unificar el patrón que ya usa
  SignalsTable).
- `RangeTabs.tsx` — pills de rango reutilizables (`1S · MTD · 1M · 3M · YTD · 1A
  · Todo`), controladas.
- `Sheet.tsx` — bottom sheet móvil (para forms/detalles; en desktop cae a Modal).
- `MetricDelta.tsx` — número + variación con color y flecha ▲▼.

**Aceptación Fase B**: los primitivos existen y están usados al menos en el hero
de cuenta (C1); `npm run build` sin errores; look más profundo y consistente.

---

## 4. FASE C — Riqueza de datos y visualización (el mayor salto)

### C1. Hero de cuenta rediseñado (`AccountSummary.tsx` → `AccountHero.tsx`)
Estilo IBKR Home/Portfolio:
- Net Liquidation **grande** (`text-4xl`), centavos atenuados
  (`$76,373` + `.40` en `text-2xl text-gray-500`).
- Debajo: delta del día en verde/rojo con % y timeframe ("hoy") — usar
  `MetricDelta`.
- Toggle **Valor | Rendimiento** (segmented control) — "Valor" muestra Net Liq;
  "Rendimiento" muestra P&L/ROI.
- A la derecha (desktop) o en fila de chips (móvil): Unrealized P&L, Realized
  P&L, Cash, Buying Power, % invertido.
- Sparkline del equity del día al lado del número.
- Preservar todos los datos de `AccountSummary` actual; solo cambia la
  presentación.

### C2. EquityChart pro (`EquityChart.tsx`)
- Área con **gradiente** (Recharts `<Area>` con `<defs><linearGradient>`).
- `RangeTabs` (B3): `1S · MTD · 1M · 3M · YTD · 1A · Todo` — filtra los datos de
  `fetchRoiHistory`.
- Tooltip rico: fecha, valor, delta vs. inicio del rango.
- `ReferenceLine` del capital aportado; marcadores de aportes/retiros si el dato
  está disponible en roi-history.
- Eje Y con labels a la derecha (estilo IBKR).
- Preservar la fuente de datos y el desglose por fondo existente.

### C3. Sparklines y asignación
- Sparkline de ROI en cada `FundCard` (usar roi-history por fondo si existe, o
  derivar de trades).
- Donut de **asignación de capital por fondo** (Recharts `<Pie>`), y opcional por
  sector (derivado de posiciones/señales).
- Barra de exposición long/short si el dato existe.

### C4. Fondos enriquecidos (`FundList.tsx` / `FundCard`)
- Migrar a `Card` (B3), tokens de color, `Stat`/`MetricDelta`.
- Sparkline de ROI, badges AUTO/CERRADO más pulidos.
- **Preservar** el click → `FundDetailModal` y todas sus acciones (aportar,
  retirar, auto-trading, cerrar). En móvil, `FundDetailModal` → `Sheet`.

### C5. Posiciones (`PositionsTable.tsx`)
- Desktop: tabla estilo IBKR — columnas `INSTRUMENTO · LAST · CHNG · POS · P&L`,
  **ordenables** (indicador ▲▼ en header), ticker bold + exchange chico gris.
  P&L en color, % de cartera.
- Móvil: **tarjetas apiladas** con fondo tintado profit/loss tenue (patrón
  "movers" de IBKR), no tabla comprimida.
- Precio en vivo que "late" al actualizar (integra con E2).
- Preservar todos los campos de `Position`.

### C6. Órdenes en lenguaje natural (`PendingOrders.tsx`)
- Reemplazar filas por **cards legibles**: ícono de estado (⏳ pendiente / ✓
  aprobada / ✕ rechazada), descripción natural ("Compra 85 WYFI a Mercado, Día"),
  valor estimado, violaciones (si hay) en rojo, fecha a la derecha.
- Preservar acciones aprobar/rechazar y la lógica de violaciones.

### C7. Radar mejorado (`SignalsTable.tsx`)
- Mantener scores por estrategia y precios live (NO tocar la lógica de datos).
- Sumar: ordenamiento por score/estrategia/sector, filtro por estrategia, y
  hacer el panel de detalle un `Sheet` en móvil.
- Migrar colores a tokens; conservar el `MiniBar` y el empty state 📡.

**Aceptación Fase C**: fondos, radar, backtest, órdenes, posiciones siguen
funcionando igual pero se ven de nivel pro; hero y equity chart estilo IBKR.

---

## 5. FASE D — Experiencia móvil profesional

### D1. Bottom tab bar (`Sidebar.tsx` → `BottomNav.tsx` en móvil)
- Reemplazar el strip lateral de 12px por barra inferior de 5 ítems (ícono +
  label, activa en `brand`), estilo IBKR. Mapeo sugerido:
  `Resumen · Fondos · Radar · Órdenes · Más`. "Más" abre un sheet con
  Posiciones/Auditoría/Backtest/Config.
- Desktop: mantener el sidebar actual (funciona bien).
- Header móvil liviano: título de sección + buscador + bell de notificaciones
  con punto de no-leídas.

### D2. Pull-to-refresh
- En el `<main>` móvil, gesto de pull-to-refresh que invalida los queries
  visibles (account, positions, signals, funds).

### D3. Sheets y tap targets
- Nueva orden y detalle de fondo → `Sheet` (bottom sheet) en móvil.
- Tap targets ≥44px en todos los controles.

### D4. Densidad adaptativa
- Confirmar que positions/orders/signals usan tarjetas en móvil (de Fase C) y no
  requieren scroll horizontal.

**Aceptación Fase D**: navegación cómoda con una mano en el celular; sin scroll
horizontal; controles críticos siempre alcanzables.

---

## 6. FASE E — Micro-interacciones y estados

### E1. Skeletons
- Reemplazar todos los `if (!data) return null` (AccountSummary, etc.) por
  `Skeleton` con la forma del contenido. Ya lo hace SignalsTable — replicar.

### E2. Flash-on-change (sensación real-time)
- Hook `useFlashOnChange(value)` que devuelve una clase de animación (flash
  verde si subió, rojo si bajó) por ~400ms. Aplicar a precios live (radar,
  posiciones) y a Net Liq/P&L cuando cambian por WebSocket/polling.
- Definir `@keyframes flash-green/flash-red` en `index.css`.

### E3. Estados vacíos diseñados
- Unificar con `EmptyState` (B3): "Sin posiciones abiertas", "Sin órdenes
  pendientes", "Sin fondos", etc., con ícono y microcopy.

### E4. Optimistic updates
- Halt/resume, aportes a fondos, aprobar/cancelar orden: reflejar el cambio al
  instante y revertir si falla (TanStack `onMutate`/`onError`/`onSettled`).

### E5. Transiciones
- Considerar `framer-motion` (nueva dep) para transiciones de sección, apertura
  de sheets, y flash. Si se agrega, `npm install framer-motion` y documentarlo.
- Alternativa sin dep: transiciones CSS (`transition`, `@keyframes`).

**Aceptación Fase E**: el panel se siente "vivo"; sin pantallas en blanco;
cambios instantáneos y reversibles.

---

## 7. ORDEN DE EJECUCIÓN Y CHECKLIST

1. **Fase A** (incluye el bug A0 del halt) — deploy + verificar en móvil.
2. **Fase B** — tokens + primitivos; deploy.
3. **Fase C** — sección por sección (hero → equity → fondos → posiciones →
   órdenes → radar), deploy tras cada componente grande.
4. **Fase E** en paralelo con C (skeletons/flash aplican a lo que se va tocando).
5. **Fase D** — móvil pro.

### Checklist de no-regresión (correr al final de cada fase)
- [ ] Login por API key funciona.
- [ ] Fondos: lista, detalle, aportar/retirar, auto-trading, cerrar.
- [ ] Radar: señales, scores, precios live, "+ Orden" precargado.
- [ ] Backtest y Walk-forward corren y muestran métricas + curva.
- [ ] Órdenes: pendientes aprobar/rechazar; nueva orden con sugerencia de tamaño.
- [ ] Posiciones se listan con P&L.
- [ ] Auditoría se ve.
- [ ] Config (reglas + señales) se edita y guarda.
- [ ] Estado: pausar/reanudar/reconectar/cambiar modo funcionan.
- [ ] WebSocket conecta; toasts y notificaciones.
- [ ] `npm run build` sin errores de TypeScript.
- [ ] Probado en móvil real.

---

## 8. NOTAS PARA SONNET
- No inventar endpoints: usar los enumerados en la sección 1. Si falta uno
  (reconnect, health), crearlo en `main.py` como se indica en Fase A.
- No romper tipos: `src/api/types.ts` es la fuente de verdad del modelo de datos.
- Cambios grandes = varios componentes; hacerlo de a uno y deployar seguido.
- Preferir migración gradual de estilos (tokens) sobre reescrituras masivas.
- Ante duda de diseño, mirar las capturas de IBKR referenciadas en
  `UI_UX_PLAN.md` (mismo directorio) y priorizar claridad + densidad glanceable.
- Responder siempre en español; texto de UI en español.
