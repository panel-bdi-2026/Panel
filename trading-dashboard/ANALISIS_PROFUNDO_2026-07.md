# Análisis profundo del sistema — 2026-07-12

> Revisión completa de todo el código (backend 11k líneas + frontend React).
> Cada hallazgo incluye una **segunda revisión** (por qué funciona o no, cuándo
> muerde, qué tan real es el impacto). Plan pensado para ejecutar con Sonnet.
>
> **Veredicto general:** el sistema está muy bien construido. El risk engine, la
> reconciliación de fills, el scoring cross-sectional y los indicadores son de
> calidad profesional. Los hallazgos NO son "está mal hecho" — son oportunidades
> concretas, y las de mayor palanca están en **la credibilidad y la mejora del
> retorno real**, no en bugs de código.

---

## PARTE 1 — RETORNO (lo más importante)

### R1. 🔴 Sesgo de supervivencia en el universo del backtest
**Qué:** `DEFAULT_UNIVERSE = _SP500_TICKERS + GROWTH_TICKERS` (screener_config.py)
es una lista **estática hardcodeada** de los constituyentes de HOY del S&P 500.
El backtest de 22 años (2003-2026) corre sobre esa lista. El equipo ya excluye
`GROWTH_TICKERS` del backtest (consciente del look-ahead), pero el `_SP500_TICKERS`
sigue siendo "los que sobrevivieron hasta hoy".

**Por qué importa (2ª revisión):** todo el edge medido (DSR, Sharpe, retornos
del walk-forward) que justifica ir a real está calculado sobre nombres que por
definición no quebraron, no fueron deslistados ni expulsados del índice. Los
perdedores históricos (Lehman, Enron, WorldCom, GE en su caída, etc.) no están.
Esto **infla el retorno del backtest** y — peor — la **optimización de parámetros**
(umbrales de entrada, rangos de RSI, MACD lookback) se ajustó sobre una muestra
sesgada, así que los "óptimos" pueden estar sobreajustados a lo que funcionó
*para los sobrevivientes*.

**¿Invalida el edge?** No del todo: momentum y quality son factores robustos en
la literatura académica con datos point-in-time. Pero la **magnitud está
sobreestimada**, probablemente de forma material (estudios típicos: 2-4% anual
de sesgo de supervivencia en universos de índice). La decisión de escalar a
capital real debe descontar esto.

**Mitigación realista (no hay datos point-in-time gratis):**
1. **La mejor mitigación práctica es R2** (tracking forward real vs. backtest) —
   bypassa el backtest sesgado midiendo el edge de verdad.
2. Documentar un "haircut de supervivencia" en las expectativas (ej. asumir
   retorno real = backtest × 0.6-0.7 hasta tener 6+ meses de forward).
3. Opcional/caro: agregar manualmente ~30-50 nombres deslistados grandes del
   período al universo de backtest para cuantificar el sesgo (correr con y sin,
   medir la diferencia). Alto esfuerzo, pero da el número exacto del haircut.

**Prioridad:** conceptual/alta. No es un fix de código de un día — es un cambio
de cómo se interpretan los resultados + priorizar R2.

---

### R2. 🔴 No hay tracking de implementation shortfall (precio teórico vs. real)
**Qué:** El Roadmap Fase 4 lo pide explícitamente ("registrar cada trade con
precio teórico vs. real de IBKR, medir el implementation shortfall") pero **no
está construido**. Hoy el audit registra la señal y el fill por separado, pero
nada computa la brecha ni la agrega.

**Por qué es la mejor inversión (2ª revisión):** es la **única fuente de verdad
que no depende del backtest sesgado (R1)**. Mide el edge real, neto de costos,
con datos que YA fluyen:
- Al draftear/ejecutar ya se tiene `signal_price` (`result.last_price`, cierre de
  la barra de la señal) y luego el `avg_fill_price` real.
- La brecha = `(avg_fill_price − signal_price) / signal_price`. Agregada, es el
  slippage real de ejecución.
- Comparar el P&L real de paper vs. el P&L que el backtest predijo para el mismo
  período responde la pregunta que más importa: **¿mi edge sobrevive a la
  ejecución real?**

**Por qué es alto ROI:** el backtest asume `slippage_pct = 0.05%`. Con datos
demorados (R3) y LMT al precio de referencia (R5), el slippage real es casi seguro
mayor. Sin medirlo, no lo sabés. Es mayormente una capa de reporting sobre datos
existentes — esfuerzo moderado, valor altísimo.

**Implementación:**
- Backend: al registrar cada fill (`funds_store.record_fill` / audit), guardar
  también `signal_price` y `signal_as_of`. Nuevo endpoint
  `GET /api/analytics/execution` que agregue: shortfall promedio, por estrategia,
  por sector, distribución, y P&L real vs. teórico por mes.
- Frontend: nueva vista "Ejecución" con el shortfall promedio, un histograma, y
  la comparación mensual real-vs-backtest.

**Prioridad:** máxima entre las de retorno. Buildeable, mide todo lo demás.

---

### R3. 🟠 Datos de mercado demorados (market_data_type=3) — selección adversa
**Qué:** `broker.py` corre con `market_data_type=3` (delayed, ~15 min). El radar
"en vivo", los precios de referencia para sizing y el `limit_price` de las
órdenes usan ese precio demorado. El punto verde pulsante del radar dice "en
vivo" pero son datos de hace 15 minutos.

**Por qué muerde el retorno (2ª revisión):** las entradas son `LMT` al precio
demorado. En un mercado subiendo (justo los nombres momentum que querés), el
precio real ya está por encima del demorado → tu LMT queda por debajo del mercado
→ **no llena**. En un mercado bajando, el demorado está por encima del real → tu
LMT llena de inmediato → **comprás justo lo que está cayendo**. Esto es
**selección adversa pura**: perdés los que suben, agarrás los que bajan. Para una
estrategia de momentum es directamente contraproducente.

**¿Vale pagar datos real-time?** Sí. IBKR US equity real-time no-pro cuesta
~US$1.50-10/mes. Para un sistema que intenta capturar momentum, elimina la
selección adversa en la entrada. Es de los fixes más baratos con impacto real
en retorno. Las cuentas paper heredan la suscripción de la cuenta live linkeada.

**Fixes:**
1. Habilitar suscripción real-time en IBKR → `ib_market_data_type=1` en config.
2. Mientras tanto (honestidad): el radar debe decir "demorado 15m", no "en vivo",
   cuando `market_data_type != 1`. Exponer el tipo de dato en `/api/status` y
   mostrarlo en el indicador del radar (hoy miente).

**Prioridad:** alta. #2 (honestidad) es de UI, gratis, hacer ya. #1 depende de
que actives la suscripción.

---

### R4. 🟠 Inconsistencia del multiplicador de convicción → rechaza las mejores señales
**Qué:** En `rules.py`, `suggested_quantity()` escala el tope de posición por
convicción (`effective_max_pos_pct = max_position_pct × conviction`, hasta 1.5×
para score=100), PERO `evaluate()` valida `position_pct` contra el
`max_position_pct_of_equity` **sin escalar**. Nota: sí se pasa
`effective_max_order_value_usd` a evaluate(), pero NO el equivalente para
max_position_pct.

**Cuándo muerde (2ª revisión):** solo cuando `qty_by_position_pct` es el binding
constraint Y score>50. Con un stop ATR ajustado, normalmente binda
`qty_by_risk`, así que no siempre pasa. PERO para nombres de **baja volatilidad**
(ATR chico → qty por riesgo grande → binda el tope de posición), una señal de
score alto se sizea a >9% y después `evaluate()` la **rechaza entera**. Resultado:
el sistema descarta sistemáticamente sus entradas de mayor convicción y menor
volatilidad — que suelen ser los mejores trades ajustados por riesgo. Perverso.

**Fix:** pasar `effective_max_position_pct_of_equity` a `evaluate()` (igual que ya
se hace con `effective_max_order_value_usd`), o cappear `suggested_quantity` al
tope sin escalar. La primera opción mantiene la intención del feature de
convicción. ~15 líneas.

**Prioridad:** media-alta. Bug sutil de retorno, real, con fix acotado.

---

### R5. 🟠 LMT exactamente al precio de referencia → no-fills crónicos en momentum
**Qué:** `_try_auto_trade_entry` y el drafter usan `order_type=LMT,
limit_price=live_price` (el precio de referencia, además demorado por R3).

**Por qué muerde (2ª revisión):** un BUY LMT exactamente al último precio no llena
si el ask está por encima (lo normal: siempre hay spread). Para nombres que se
mueven hacia arriba, el LMT al last queda por debajo del ask y **nunca llena**
hasta que el precio baje a tu límite. Combinado con R3 (precio demorado), la
entrada tiene doble desventaja. El backtest asume que entrás; en vivo, perdés
justo las entradas que estaban corriendo a tu favor.

**Fix:** usar un **límite marketable** — límite = precio × (1 + buffer), ej.
+0.2-0.3%, o precio del ask + un tick. Llena de forma confiable acotando el
slippage al buffer. R2 después te dice si el buffer elegido es razonable.

**¿Por qué no market order directo?** Un MKT en un nombre poco líquido puede
sufrir slippage grande sin tope. El límite marketable da lo mejor de ambos:
llena casi siempre pero con un techo de precio. Correcto para este caso.

**Prioridad:** alta. Junto con R2+R3, es el trío que más mueve el retorno real.

---

### R6. 🟡 Asignación FIFO entre fondos de la misma estrategia (sin rotación)
**Qué:** documentado en el código: con 2+ fondos activos en la misma estrategia,
el fondo más viejo (orden de creación) absorbe cada señal mientras tenga cash;
los nuevos solo reciben lo que el primero no pudo pagar. Sin reparto proporcional
ni round-robin.

**2ª revisión:** hoy no muerde (un fondo activo por estrategia). Pero el Roadmap
3.3 quiere activar Momentum en paralelo a Oportunista — ahí sí importaría si
alguna vez hay 2 fondos en la misma estrategia. Reparto proporcional al cash
disponible sería lo correcto.

**Prioridad:** baja hoy, media si se activa la diversificación de fondos.

---

## PARTE 2 — INFORMACIÓN Y UX QUE MEJORAN DECISIONES

### I1. 🟠 El "por qué" de una señal no se muestra al usuario
**Qué:** `SignalResult` tiene `score_components` (el desglose por factor:
relative_strength, momentum_12_1, trend, rsi, macd, bollinger, sector_rs) y
`notes` (por qué falló filtros), pero el radar solo muestra las 4 barras de score
por estrategia. El desglose de POR QUÉ un símbolo puntúa alto o falla no se
expone.

**Por qué mejora el retorno (2ª revisión):** ver qué factores impulsan cada
señal te deja (a) sanity-checkear los auto-trades antes de aprobarlos, (b)
aprender qué factores están funcionando en el régimen actual, (c) detectar
señales "frágiles" (score alto por un solo componente extremo, que la
normalización cross-sectional debería mitigar pero conviene ver). Información que
YA se computa y se tira.

**Fix:** en el panel de detalle del radar, mostrar el `score_components`
normalizado como mini-barras + los `notes`. Bajo esfuerzo (el dato ya viaja en
la señal o se agrega al endpoint).

**Prioridad:** media-alta. Barato, mejora decisiones.

### I2. 🟠 Sin vista de atribución de performance / trade journal
**Qué:** no hay una vista que responda "¿qué está funcionando?": win rate por
estrategia, por sector, por factor; P&L por motivo de salida (stop vs. time vs.
trend-break vs. take-profit); avg win/loss real. El backtest lo calcula, pero el
trading real no tiene su espejo.

**Por qué (2ª revisión):** sin atribución no podés mejorar el retorno de forma
dirigida — no sabés si perdés por malas entradas, malas salidas, un sector, o un
régimen. Los datos están en el audit log y en `fund.trades`. Es una capa de
agregación + visualización.

**Prioridad:** media. Se apoya en R2 (mismo pipeline de analytics).

### I3. 🟡 Detalle por fondo pobre en la nueva UI
El `FundDetailModal` existe pero podría mostrar: posiciones vivas con P&L, curva
de equity del fondo (ya hay `EquityChart fundId`), historial de trades con motivo
de salida, y el shortfall del fondo (post R2). Media.

---

## PARTE 3 — ARQUITECTURA / ROBUSTEZ

### A1. 🟠 Reconexión automática a IBKR (ya en Roadmap, subir prioridad)
**Qué:** el backend conecta a IBKR una sola vez al arrancar. Si se cae (reinicio
nocturno ~23:45 ET, caída de red, restart semanal con 2FA) queda desconectado
hasta reinicio manual. El `POST /api/reconnect` que se agregó (2026-07-11) cubre
la emergencia pero requiere acción humana.

**2ª revisión:** confirmado necesario **dos veces** en la sesión del 11-12/07
(IBKR se desconectó solo). Un loop de background que cada 60s chequee
`ib.isConnected()` y reconecte si hace falta lo resuelve de raíz. **Riesgo si NO
se hace:** el sistema puede pasar horas desconectado sin que nadie lo note,
perdiendo señales y sin poder cerrar posiciones por time/trend exit (aunque el
stop bracket en IBKR sigue protegiendo). Para un sistema que aspira a operar
desatendido, es la brecha de robustez #1.

**Fix:** loop async con backoff; al reconectar, re-suscribir el hot-set y correr
`_reconcile_unfilled_on_startup`. Prioridad: **alta**.

### A2. 🟡 main.py — monolito de 3.547 líneas (Roadmap 2.1)
Cada cambio tiene efectos secundarios difíciles de prever. El plan de partirlo en
`api/`, `services/`, `state.py` sigue vigente. **2ª revisión:** es deuda técnica
real pero NO urgente — el código funciona y está bien testeado. Hacerlo
incremental, un módulo a la vez, con tests verdes. Prioridad: media, hacer de a
poco entre features.

### A3. 🟡 Estado en RAM → SQLite (Roadmap 2.2, parcialmente hecho)
`session_store.py` y `state_store.py` existen (sesiones y signal_state ya
persisten). Verificar qué queda en RAM que un restart borre (peak_equity_usd,
pending_orders ya persisten en state.json). Auditar y cerrar. Prioridad: media.

### A4. 🟢 Comentarios obsoletos
`screener.py` menciona "API gratuita de Yahoo Finance" pero ya migraron a Tiingo.
Cosmético; corregir al pasar.

---

## PARTE 4 — SEGURIDAD
La postura es correcta y ya está razonada en el Roadmap: Tailscale como capa de
red, API key estática, rate limiting con slowapi (ya implementado — el Roadmap
lo lista como "hacer ahora" pero YA ESTÁ, doc desactualizada). JWT/HTTPS
descartados con buen criterio. **Sin hallazgos de seguridad nuevos.** Único ítem:
el `.env` con la API key y el token de Tiingo — confirmar permisos 600 y que no
esté en git (ya cubierto por el `.gitignore` nuevo).

---

## RESUMEN EJECUTIVO Y PLAN DE ACCIÓN

### Lo que está muy bien (no tocar)
Risk engine (portfolio heat, sector, exposición), reconciliación de fills tardíos
y stops rechazados, scoring cross-sectional por percentiles, indicadores
académicos, filtro de régimen dual-momentum, exits por estrategia. Calidad
profesional.

### Los 3 temas que más mueven el retorno real (hacer primero, en orden)
1. **R2 — Tracking de implementation shortfall** (real vs. teórico). La brújula
   que mide todo lo demás y bypassa el sesgo del backtest. Esfuerzo medio, valor
   máximo.
2. **R5 + R3 — Ejecución**: límite marketable + honestidad sobre datos demorados
   (y habilitar real-time si es viable). Ataca la selección adversa en la entrada.
3. **R4 — Fix del multiplicador de convicción** en evaluate(). Deja de rechazar
   las mejores señales de baja volatilidad. Fix acotado.

### Robustez (hacer en paralelo)
4. **A1 — Reconexión automática a IBKR.** Brecha #1 para operar desatendido;
   confirmada necesaria 2× esta semana.

### Información/UX (mejoran decisiones y aprendizaje)
5. **I1 — Mostrar el "por qué" de las señales** (score_components + notes).
6. **I2 — Vista de atribución de performance** (se apoya en el pipeline de R2).

### Conceptual (no es código, es interpretación)
7. **R1 — Sesgo de supervivencia**: descontar expectativas hasta tener forward
   real (R2). Opcional: cuantificarlo agregando deslistados al backtest.

### Deuda técnica (incremental, sin urgencia)
8. A2 (partir main.py), A3 (auditar estado en RAM), A4 (comentarios), R6 (rotación
   de fondos si se diversifica).

---

## APÉNDICE — PLAN DE IMPLEMENTACIÓN PARA SONNET (orden sugerido)

> Cada bloque: archivos, cambios concretos, criterio de aceptación. Deploy del
> backend = `git pull` + `systemctl restart trading-dashboard`; del frontend =
> `npm run build` en `frontend/`. Preservar TODA la funcionalidad existente.

### Bloque 1 — R4: fix del multiplicador de convicción (backend, ~1h)
- `rules.py::evaluate()`: agregar parámetro `effective_max_position_pct: float |
  None = None`; usarlo en el chequeo de `max_position_pct_of_equity` en vez de
  `self.config.max_position_pct_of_equity` cuando venga.
- `main.py`: en `_try_auto_trade_entry` y `_draft_fund_order_from_signal`, pasar
  `effective_max_position_pct = max_position_pct × conviction_multiplier` (mismo
  patrón que `effective_max_order_usd`).
- Aceptación: una señal score=90 en un nombre de baja volatilidad ya no se
  rechaza por `max_position_pct` cuando su sizing quedó por encima del tope
  nominal pero dentro del escalado. Tests de rules.py verdes.

### Bloque 2 — R5+R3: ejecución (backend + UI honestidad, ~medio día)
- `config.py`: nuevo `order_limit_buffer_pct` (default 0.25).
- `main.py` (ambos paths de orden): `limit_price = round(live_price × (1 +
  buffer/100), 2)` para BUY. Documentar por qué (límite marketable).
- `/api/status`: exponer `market_data_type`. Frontend `SignalsTable`: si != 1,
  el indicador dice "demorado 15m" (gris), no el punto verde "en vivo".
- Aceptación: las órdenes drafteadas muestran un límite ligeramente sobre el
  precio; el radar deja de afirmar "en vivo" con datos demorados.

### Bloque 3 — R2: implementation shortfall (backend + nueva vista, ~2 días)
- Al registrar fills (`funds_store.record_fill` y/o audit), persistir
  `signal_price` (= `result.last_price` de la señal que originó la orden) y
  `signal_as_of`. Propagarlo desde `_try_auto_trade_entry`/drafter.
- Nuevo `GET /api/analytics/execution`: por trade y agregado — shortfall
  `(fill − signal)/signal`, promedio, por estrategia/sector, distribución, y
  comparación P&L real vs. teórico por mes.
- Frontend: nueva sección "Ejecución" (nav) con KPIs + histograma de shortfall +
  tabla mensual real-vs-backtest.
- Aceptación: después de N trades, la vista muestra el slippage real y si supera
  el 0.05% que asume el backtest.

### Bloque 4 — A1: reconexión automática IBKR (backend, ~medio día)
- `main.py`: loop async `_ib_reconnect_loop` cada 60s: si `not
  broker.is_connected()` y no está en medio de un cambio de modo, llamar
  `broker.reconnect(...)`; al éxito, re-suscribir hot-set + `_reconcile_
  unfilled_on_startup`. Backoff si falla repetidamente. Registrar en audit.
- Aceptación: matar IB Gateway y verificar que el backend reconecta solo en <2min
  sin intervención.

### Bloque 5 — I1: "por qué" de las señales (backend expone + frontend, ~medio día)
- Asegurar que `/api/signals/scan/all` incluya `score_components` y `notes` por
  símbolo (o un endpoint de detalle por símbolo).
- Frontend `SignalsTable` panel de detalle: mini-barras de cada componente
  normalizado + lista de `notes`.
- Aceptación: al abrir una señal se ve qué factores la impulsan y por qué
  pasa/falla filtros.

### Bloque 6 — I2: atribución de performance (backend + frontend, ~1-2 días)
- Endpoint `GET /api/analytics/performance`: win rate y P&L por estrategia /
  sector / motivo de salida, sobre `fund.trades` + audit.
- Frontend: vista con esos cortes.
- Aceptación: se puede responder "¿qué estrategia/sector/tipo de salida aporta o
  drena retorno?".

### Bloque 7+ — deuda técnica (incremental)
A2 (partir main.py módulo a módulo), A3 (auditar RAM), R6 (rotación de fondos),
A4 (comentarios). Sin urgencia; entre features.
