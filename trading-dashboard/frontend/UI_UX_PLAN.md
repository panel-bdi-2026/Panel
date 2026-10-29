# Plan de UI/UX — Trading Dashboard → nivel "app IBKR"

> Objetivo: llevar el dashboard de "panel funcional" a "producto pulido y
> premium", con foco en confiabilidad de controles críticos, densidad de
> información bien jerarquizada, sensación real-time, y una experiencia móvil
> de nivel profesional. Construir SOBRE el stack actual (React 19 + Vite +
> Tailwind + TanStack Query + Zustand + Recharts + lucide), no reemplazarlo.

## Estado actual (diagnóstico)

Fortalezas:
- Stack moderno y bien organizado (componentes por dominio, hooks, stores).
- WebSocket para realtime, TanStack Query para polling/cache.
- Ya responsive (sidebar desktop + strip/drawer móvil).

Debilidades de UI/UX:
- Estética plana y genérica: paleta `gray-950/900` de Tailwind sin sistema de
  elevación ni profundidad; acento azul por defecto sin identidad.
- Densidad de datos "cruda": casi todo son tablas y filas de texto chico. Poca
  visualización (solo un EquityChart). Sin sparklines, sin donut de asignación,
  sin mini-gráficos por posición.
- Sin sensación real-time: los números se actualizan pero no "laten" (no hay
  flash verde/rojo al cambiar un precio o P&L).
- Estados pobres: `if (!data) return null` en todos lados → pantallas en blanco
  mientras carga, en vez de skeletons. Sin estados vacíos diseñados.
- **Controles críticos frágiles en móvil** (ver incidente del 2026-07-11).
- Navegación móvil por strip de íconos lateral (12px de ancho) — poco ergonómica
  vs. una bottom-tab bar estándar de apps financieras.

---

## Patrones concretos a adoptar del IBKR GlobalTrader (de las capturas)

Referencias visuales que el usuario compartió (app IBKR móvil). DNA de diseño a
copiar/adaptar — nota: IBKR usa tema claro; nosotros mantenemos oscuro por
defecto (trading nocturno) pero adoptamos la estructura y jerarquía. Se puede
ofrecer toggle claro/oscuro más adelante.

1. **Hero number** protagonista: valor enorme con los centavos atenuados
   (`$76,373.40` con `.40` más chico), y debajo el delta en verde/rojo con % y
   un timeframe explícito ("past week"). Toggle **Value | Performance** arriba.
2. **Área chart con gradiente** bajo la línea + **pills de rango exactas**:
   `1W · MTD · 1M · 3M · YTD · 1Y · All` (la activa con fondo tenue).
3. **Bottom tab bar** de 5 (Home · Portfolio · Trade · Watchlists · Markets),
   ícono + label, activa en color. Confirma D1.
4. **Fila de quick-action pills** redondeadas bajo el hero (+ · Deposit ·
   PortfolioAnalyst…). → Patrón ideal para nuestros **controles críticos**
   (Reanudar/Pausar · Reconectar IBKR · Nueva orden) — ver Fase A.
5. **Tabs horizontales dentro de la pantalla** (Positions · Balances · Orders ·
   AI Instructions) con subrayado en la activa, scrolleables.
6. **Filas de instrumento**: ticker en **bold** + exchange (NYSE/NASDAQ) chico
   en gris debajo/al lado. Columnas `LAST · CHNG · POS · P&L` **ordenables**
   (triangulito en el header). P&L coloreado.
7. **Tiles de movers/favoritos con fondo tintado** según signo (fondo rojo tenue
   para AMZN −0.56%). Glanceable al instante.
8. **Cards horizontales** "Recently Viewed" (logo, ticker, precio, % en pill) —
   scroll horizontal.
9. **Cards de orden en lenguaje natural**: ícono de estado (✓ en círculo),
   "Bought 85 WYFI Market, Day", "85 Filled, Avg. Price: $36.78", fecha a la
   derecha. Mucho más legible que una fila de tabla.
10. **Fila de contadores** con tap-through: `0 Orders / 1 Trades / 0 Recurring`.
11. **Bell de notificaciones** con punto azul de no-leídas; buscador global.

Cómo se mapea a las fases: #1/#2 → C1/C3 · #3 → D1 · #4 → A3 · #6/#7 → C4 ·
#9 → C (órdenes) · #5/#8/#10/#11 → mejoras transversales.

---

## FASE A — Controles críticos y confiabilidad (PRIORIDAD MÁXIMA)

> Motivada por un incidente real: IBKR se desconectó, el trading quedó en halt,
> y desde el celular no había forma de recuperar el sistema. El botón "HALTED"
> existe pero (a) es un badge chico poco evidente como acción, y (b) no hay
> ningún control para **reconectar IBKR** — la única reconexión es reiniciar el
> backend por SSH o togglear modo live/paper (peligroso). Un usuario móvil queda
> sin salida.

### A1. Endpoint + botón de "Reconectar IBKR"
- Backend: agregar `POST /api/reconnect` (reutiliza `broker.reconnect(...)` que
  ya existe, usado hoy solo en startup y en `/api/mode`). Idempotente, sin tocar
  el modo ni el halt.
- Frontend: cuando `status.connected === false`, mostrar un botón prominente
  "Reconectar IBKR" (no un badge escondido).

### A2. "System Health" banner accionable
- Cuando `health.status !== 'ok'`, mostrar una barra/tarjeta arriba de todo con
  los issues activos (`ibkr_disconnected`, `trading_halted`, `scan_stale`) y un
  botón de acción por cada uno: Reconectar / Reanudar / Ver scan.
- Hoy esos issues solo se ven como badges dispersos o no se ven en móvil.

### A3. Barra de control crítico persistente (patrón "quick-action pills" de IBKR)
- Adoptar el patrón de la fila de pills redondeadas que IBKR pone bajo el hero,
  pero para NUESTROS controles críticos: `▶ Reanudar / ⏸ Pausar` · `🔌 Reconectar
  IBKR` · `+ Nueva orden`. Tap targets grandes (≥44px), siempre visibles, nunca
  escondidos detrás de `hidden sm:block`.
- Estado del sistema (🟢/🟡/🔴) a la izquierda de las pills.
- Confirmación clara al reanudar ("Reanudar trading en modo PAPER/LIVE?").

### A4. Feedback honesto de estado tras una acción
- Hoy `toggleHalt` solo invalida el query; si el sistema sigue degradado por
  IBKR, parece que "no funcionó". Mostrar el resultado real: "Trading reanudado,
  pero IBKR sigue desconectado — reconectá para reanudar el scan."

---

## FASE B — Sistema de diseño (fundación visual)

> Sin esto, cualquier mejora visual queda inconsistente. Es la base del salto de
> calidad.

### B1. Tokens de diseño (Tailwind config)
- Paleta de superficies con **elevación real**: `surface-0/1/2/3` (fondo,
  tarjeta, tarjeta elevada, overlay) en vez de gray-950/900 planos.
- Color semántico consistente: `profit`/`loss` (verde/rojo con variantes),
  `long`/`short`, `warn`, y un acento de marca distintivo (no el azul default).
- Escala de espaciado y radios consistente; sombras sutiles para profundidad.

### B2. Tipografía financiera
- Números tabulares en TODA cifra monetaria/porcentual (`tabular-nums`) — hoy
  solo en un lugar. Evita que los números "bailen" al actualizarse.
- Jerarquía clara: hero numbers grandes y con peso, labels chicos en mayúsculas
  y color atenuado. Considerar una fuente tipo Inter/Geist para pulido.

### B3. Componentes UI ampliados
- Estandarizar `Card`, `Stat`, `Skeleton`, `EmptyState`, `MetricDelta` (número +
  flecha + color), `Sparkline`. Hoy hay Badge/Button/Modal/StatusDot/Toast, falta
  la capa de composición de datos.

---

## FASE C — Riqueza de datos y visualización

> Lo que más diferencia un panel "de programador" de una app financiera premium.

### C1. Hero de cuenta rediseñado (estilo IBKR)
- Net Liquidation como número protagonista **grande con centavos atenuados**
  (`$76,373`.40), y debajo el delta en verde/rojo con % y timeframe explícito
  (ej. "hoy" / "esta semana"), como el "past week" de IBKR.
- Toggle **Valor | Rendimiento** (equivalente al "Value | Performance").
- Unrealized/Realized P&L a la derecha (verde/rojo), como en la pantalla de
  Portfolio de IBKR.
- Mini-métricas secundarias (Cash, Buying Power, % invertido) como chips.

### C2. Sparklines y micro-gráficos everywhere
- Sparkline de 7/30 días en cada fondo (FundList) y en cada posición.
- Donut de **asignación por fondo** y por sector/símbolo.
- Barra de exposición long/short.

### C3. EquityChart de nivel pro (calcado del Home de IBKR)
- Área con **gradiente** bajo la línea, tooltip rico (fecha, valor, delta vs.
  inicio), y **pills de rango idénticas a IBKR**: `1S · MTD · 1M · 3M · YTD ·
  1A · Todo` (la activa con fondo tenue).
- Línea de referencia del capital aportado, marcadores de aportes/retiros.
- Eje Y con labels de valor a la derecha (como IBKR: 75K–77K).

### C4. Posiciones enriquecidas (estilo tabla IBKR + tarjetas móvil)
- Desktop: tabla con columnas tipo IBKR `INSTRUMENTO · LAST · CHNG · POS · P&L`,
  **ordenables** (indicador de orden en el header). Ticker en bold + exchange
  chico en gris.
- Cada posición con: P&L no realizado en color, % de la cartera, mini-sparkline
  de precio, badge long/short, y precio en vivo que "late" al actualizar (E2).
- Móvil: **tarjetas apiladas con fondo tintado** verde/rojo tenue según P&L del
  día (patrón de "movers" de IBKR), no una tabla comprimida.

### C5. Órdenes en lenguaje natural (cards de IBKR)
- Reemplazar filas de tabla por **cards legibles**: ícono de estado (✓/⏳/✕),
  descripción en lenguaje natural ("Compró 85 WYFI a Mercado, Día · 85 llenada,
  precio prom. $36.78"), fecha a la derecha. Sort "Más reciente".

### C6. Señales más legibles
- ScoreBar ya existe; sumar visualización del "por qué" (qué reglas pasan) de
  forma glanceable, y ordenamiento/filtro por score, estrategia, sector.

---

## FASE D — Experiencia móvil de nivel profesional

### D1. Bottom tab bar (como IBKR GlobalTrader)
- Reemplazar el strip lateral de 12px por una barra inferior estándar de 5
  ítems (ícono + label, activa en color de marca), tal cual IBKR (Home ·
  Portfolio · Trade · Watchlists · Markets). Mapeo a nuestras secciones:
  ej. `Resumen · Fondos · Señales · Órdenes · Más`.
- Header superior liviano con título de sección, buscador y bell de
  notificaciones con punto de no-leídas (patrón IBKR).

### D2. Pull-to-refresh
- Gesto estándar para forzar refetch de account/positions/signals.

### D3. Gestos y tap targets
- Swipe entre secciones; tap targets ≥44px; sheets (bottom sheets) para el form
  de nueva orden y detalle de fondo en vez de modales centrados.

### D4. Densidad adaptativa
- Tablas → tarjetas apiladas en móvil (positions, orders, signals) para evitar
  scroll horizontal y texto de 10px.

---

## FASE E — Micro-interacciones y estados

### E1. Skeleton loaders
- Reemplazar todos los `if (!data) return null` por skeletons con la forma del
  contenido. Elimina los flashes en blanco.

### E2. Flash-on-change (sensación real-time)
- Cuando un precio/P&L/net-liq cambia vía WebSocket, animar un flash verde/rojo
  breve. Es EL detalle que hace sentir "vivo" a un panel de trading.

### E3. Estados vacíos diseñados
- "Sin posiciones abiertas", "No hay señales que pasen los filtros hoy", etc.,
  con ícono y microcopy, en vez de tablas vacías.

### E4. Optimistic updates + toasts pulidos
- Halt/resume, aportes a fondos, cancelar orden: reflejar el cambio al instante
  y revertir si falla, con toast claro.

### E5. Transiciones
- Transiciones suaves al cambiar de sección, abrir sheets, y en cambios de
  estado. Nada brusco.

---

## Secuenciación recomendada

1. **Fase A** (controles críticos) — arreglar primero lo que te dejó sin poder
   operar desde el celular. Bajo esfuerzo, alto impacto de seguridad.
2. **Fase B** (sistema de diseño) — fundación; sin esto lo demás queda parche.
3. **Fase C + E** en paralelo (viz + micro-interacciones) — el grueso del salto
   de calidad percibida.
4. **Fase D** (móvil pro) — puede solaparse con C/E.

## Notas de implementación
- Todo incremental y sección por sección; el dashboard sigue usable en cada paso.
- Deploy del frontend: `cd frontend && npm run build` (genera `dist/`, servido por
  el backend vía StaticFiles). Verificar en móvil real, no solo devtools.
- Considerar `framer-motion` para animaciones (flash-on-change, transiciones,
  sheets) — se integra limpio con React 19.
