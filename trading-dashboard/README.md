# Trading Dashboard — Interactive Brokers

Dashboard para ver tu cuenta de IBKR (posiciones, cuenta, P&L) y enviar órdenes
de compra/venta **a través de un motor de reglas de riesgo**, en vez de hablar
directo contra el broker. Es un gateway de ejecución con guardarraíles, no un
robot de estrategias: tú (o quien envíe la orden) decides qué comprar/vender;
el sistema se encarga de que esa orden no rompa los límites que definiste.

> ⚠️ **Esto puede mover dinero real.** Lee la sección "Modelo de seguridad"
> completa antes de conectar una cuenta live. Empieza siempre en `paper`.

## Arquitectura

```
trading-dashboard/
├── backend/
│   ├── app/
│   │   ├── config.py    # variables de entorno, validación de modo live
│   │   ├── models.py    # esquemas (OrderRequest, AccountSummary, etc.)
│   │   ├── rules.py     # RulesEngine: aprueba/rechaza/escala cada orden
│   │   ├── audit.py     # bitácora SQLite de todo lo que toca dinero
│   │   ├── broker.py    # adaptador a IBKR vía ib_async
│   │   └── main.py      # API FastAPI + WebSocket + sirve el frontend
│   │   ├── indicators.py # SMA, RSI, ROC, ATR — funciones puras, sin red
│   │   ├── market_data.py# datos de precios (Yahoo Finance via yfinance, gratis)
│   │   ├── screener.py   # MomentumScreener: rankea candidatos del universo
│   │   ├── screener_config.py # universo + parametros de la estrategia
│   │   └── backtest.py   # backtest simplificado de la estrategia momentum
│   ├── tests/
│   ├── rules.yaml        # límites de riesgo (editable)
│   ├── screener.yaml     # universo y parámetros del radar de oportunidades
│   ├── start_dashboard.py # lanzador: crea .env de prueba, detecta IP LAN, QR
│   ├── start_windows.bat  # doble click en Windows -> instala y abre todo
│   └── .env.example
└── frontend/
    └── index.html        # dashboard (vanilla JS, sin build step)
```

El backend se conecta a **TWS o IB Gateway** (deben estar corriendo en algún
lugar accesible — tu laptop, una VM, etc.) usando `ib_async`. El frontend habla
solo con este backend (REST + WebSocket), nunca directo con IBKR.

## Modelo de seguridad

1. **Paper primero, siempre.** `TRADING_MODE` por defecto es `paper` y el
   puerto por defecto (`7497`) es el de TWS Paper Trading. Habilitar la
   *posibilidad* de operar en live requiere definir
   `LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY` en el `.env` y reiniciar
   el backend — un paso único, deliberado, que se hace en el servidor, no
   desde el dashboard. Una vez habilitado, el botón "Activar modo LIVE" del
   dashboard permite alternar entre paper y live sin reiniciar (pide
   confirmación y `X-API-Key`, y pausa el trading automáticamente después de
   cada cambio hasta que lo reanudás a mano). Ver "Pasar a cuenta real" más
   abajo.
2. **Toda orden pasa por el `RulesEngine`** (`backend/app/rules.py`) antes de
   llegar a IBKR. Si una sola regla falla, la orden se rechaza con el detalle
   de qué regla y por qué.
3. **Lista blanca de símbolos sincronizada con el universo del screener.**
   `symbol_whitelist` en `rules.yaml` ya no se mantiene a mano por separado:
   el backend la sobrescribe automáticamente para que sea siempre igual al
   `universe` de `screener.yaml` (el radar de oportunidades, ver más abajo),
   tanto al arrancar como cada vez que cambiás esa config vía
   `PUT /api/signals/config`. Así el screener puede identificar candidatos de
   forma autónoma sobre todo el universo configurado sin que tengas que ir
   agregando símbolos uno por uno. La traba de seguridad de fondo sigue
   intacta: si el universo quedara vacío, la whitelist también queda vacía y
   no se puede operar nada — sin excepción.
4. **Aprobación manual por monto.** Aunque el modo de ejecución sea
   "automático", toda orden con valor estimado mayor a
   `manual_approval_threshold_usd` queda en una cola de pendientes — no se
   ejecuta sola. Tú apruebas o rechazas desde el dashboard.
5. **Kill switch.** El botón "Pausar trading" del dashboard bloquea cualquier
   orden nueva de inmediato. También se activa solo si la pérdida del día
   alcanza `daily_loss_limit_pct` — tanto al evaluar una orden nueva como de
   forma continua en background (no hace falta enviar una orden para que el
   backend note que se llegó al límite y pause el trading).
6. **Stop-loss obligatorio en compras** (configurable, pero viene activo por
   defecto), con un tope de riesgo (`max_stop_loss_pct`), y verificación de
   que IBKR lo aceptó: si el stop-loss es rechazado/cancelado por el broker
   después de enviar la orden padre, el backend lo trata como una falla
   crítica (no como una orden exitosa) y queda registrado en la auditoría.
7. **Short selling deshabilitado por defecto** (`allow_short_selling`): una
   orden que dejaría una posición en negativo se rechaza, para evitar quedar
   corto por error de cantidad o de símbolo.
8. **Auditoría inmutable.** Cada intento de orden (aprobada, rechazada,
   pendiente, ejecutada) queda en `audit.db` con timestamp y el resultado
   completo de la evaluación de reglas.
9. **Endpoints que mueven dinero, cambian reglas o leen datos de la cuenta
   requieren autenticación** (enviar/aprobar/rechazar órdenes, cambiar
   `rules.yaml`, pausar/reanudar, y también leer cuenta/posiciones/auditoría —
   nada de eso es público en la red local). El dashboard la resuelve con una
   pantalla de login: la contraseña (`API_KEY` en el `.env`) se valida en
   `POST /api/login`, que abre una sesión y la deja en una cookie `httpOnly`
   (no la puede leer JavaScript, ni queda en `localStorage`). El header
   `X-API-Key` de siempre sigue funcionando igual para scripts/automatización.
   El WebSocket de actualizaciones en vivo acepta la misma cookie de sesión
   (el navegador la manda sola en el handshake) o, como antes, la key como
   `?api_key=` en la URL.
10. **El estado (`halted`, `mode`, órdenes pendientes) persiste en
    `state.json`** y sobrevive a un reinicio del backend — un reinicio no
    vuelve a dejar el trading activo silenciosamente si lo habías pausado.
11. **El escaneo proactivo nunca ejecuta nada por sí solo, salvo en fondos con
    auto-trading activado.** Si lo habilitás (`auto_scan_enabled` en
    `screener.yaml`), detecta señales nuevas en background y arma órdenes de
    compra en *borrador* — pasan por el mismo `RulesEngine` que cualquier
    orden y quedan en la cola de aprobación manual, sin importar su valor
    estimado. La única excepción deliberada es un fondo con
    `auto_trading_enabled = true`: ahí la compra/venta se ejecuta sin
    aprobación manual, pero solo en modo `paper` (ver punto 16 y "Motor de
    auto-trading por fondo" más abajo). Ver también "Escaneo proactivo y
    órdenes en borrador automáticas".
12. **Símbolos validados estrictamente.** El símbolo de toda orden se valida
    contra `^[A-Z0-9.\-]{1,12}$` antes de guardarse o mostrarse, y el frontend
    escapa todo texto dinámico antes de renderizarlo. Sin esto, un "símbolo"
    con HTML/JavaScript podía quedar persistido y ejecutarse en el navegador
    de quien abriera el dashboard (XSS almacenado capaz de operar la cuenta
    con la sesión activa de quien lo abre, aunque ya no de robar la cookie de
    sesión en sí: es `httpOnly`).
13. **Superficie de red mínima.** Escanear el mercado y correr el backtest
    también requieren autenticación (no mueven dinero, pero consumen la cuota de
    la API de datos y serían un vector de DoS si quedaran abiertos). CORS está
    cerrado por defecto (el dashboard se sirve del mismo origen que la API);
    se abre solo si definís `ALLOWED_ORIGINS` en el `.env`.
14. **Ledger de fondos basado en propiedad, no en snapshots.** Si una orden se
    ata a un `fund_id` (ver "Fondos" más abajo), una venta nunca puede superar
    la cantidad que ese fondo registra como propia en su ledger — sin importar
    cuánto haya realmente en la cuenta de IBKR. Así, holdings preexistentes o
    de otro fondo en el mismo símbolo quedan protegidos automáticamente: ese
    fondo simplemente no los "ve" como suyos.
15. **Sin sobre-asignación de cash entre fondos.** Cada aporte de capital a un
    fondo (al crearlo o después) se valida contra el cash real de la cuenta
    de IBKR: la suma de `cash_usd` de todos los fondos nunca puede superar
    ese cash real. Sin esto, dos fondos podrían "creer" tener disponible el
    mismo dinero real, rompiendo la separación que el ledger promete.
16. **El motor de auto-trading por fondo nunca opera en `live`.** Tanto la
    entrada automática como el monitor de salida chequean explícitamente que
    el modo activo sea `paper` antes de hacer nada, sin importar el toggle
    `auto_trading_enabled` del fondo — para activar trading sin aprobación
    manual con dinero real haría falta cambiar ese chequeo a propósito en el
    código, no alcanza con un toggle del dashboard.

Ninguna de estas reglas reemplaza tu propio criterio. Esto no es una
recomendación de inversión ni una garantía de que una orden "aprobada" sea una
buena idea — solo que respeta los límites que tú configuraste.

## Reglas configurables (`backend/rules.yaml`)

| Campo | Qué hace |
|---|---|
| `symbol_whitelist` | Símbolos permitidos. Vacío = nada permitido. Se sobrescribe automáticamente con el `universe` de `screener.yaml`: editalo ahí, no acá. |
| `max_order_value_usd` | Valor máximo (USD) de una orden individual. |
| `max_position_pct_of_equity` | Tamaño máximo de una posición como % del NetLiquidation. |
| `daily_loss_limit_pct` | Pérdida diaria que activa el kill switch automático. |
| `max_trades_per_day` | Tope de operaciones ejecutadas por día. |
| `require_stop_loss_on_buy` | Exige stop-loss en toda compra. |
| `max_stop_loss_pct` | Riesgo máximo aceptado en ese stop-loss. |
| `allow_short_selling` | Si es `false` (default), rechaza órdenes que dejarían una posición en negativo. |
| `manual_approval_threshold_usd` | Por encima de este monto, la orden queda pendiente de tu aprobación. |
| `allow_extended_hours` / `trading_hours_*` | Restringe operar a horario regular de mercado. |
| `risk_per_trade_pct` | % del equity que se está dispuesto a perder si se toca el stop-loss. Solo se usa para *sugerir* una cantidad (ver abajo); no rechaza órdenes por sí solo — el tamaño final igual queda limitado por `max_position_pct_of_equity` y `max_order_value_usd`. |

Puedes editar el archivo directamente (requiere reiniciar el backend) o vía
`PUT /api/rules` con tu `X-API-Key`.

### Sugerencia de cantidad por riesgo

El botón **"📐 Sugerir"** junto al campo de cantidad del formulario de orden
llama a `GET /api/orders/size-suggestion` (requiere `X-API-Key`) y completa el
campo con la cantidad calculada para que, si se toca el stop-loss ingresado,
la pérdida no supere `risk_per_trade_pct` del equity actual — recortada
además por `max_position_pct_of_equity` y `max_order_value_usd` para no
sugerir algo que el `RulesEngine` rechazaría de todas formas. Si el formulario
tiene un fondo seleccionado, el endpoint recibe su `fund_id` y dimensiona
contra el `equity_estimate()` de ese fondo en vez del equity de toda la
cuenta. Es una sugerencia editable: no se aplica sola ni se envía ninguna
orden por esto.

## Radar de oportunidades (`backend/screener.yaml`)

Capa opcional de análisis que **rankea candidatos de un universo de acciones
por una estrategia momentum/técnica** (tendencia + fuerza relativa vs. el
mercado + RSI + liquidez), pensada para swing trading (días-semanas) en
acciones de EEUU. No ejecuta nada por su cuenta: solo sugiere. Cualquier orden
que decidas enviar a partir de un candidato pasa exactamente por el mismo
`RulesEngine` que cualquier otra orden — whitelist, stop-loss, límites de
tamaño, todo aplica igual.

Por defecto `universe` es el **S&P 500 completo** (503 símbolos): el screener
identifica oportunidades de forma autónoma en todo el índice en vez de
depender de que vayas cargando a mano cuáles símbolos seguir. Como la
whitelist de `rules.yaml` se sincroniza automáticamente con este universo
(ver "Modelo de seguridad", punto 3), ampliarlo o recortarlo acá también
cambia qué símbolos pueden operarse. Podés acortarlo en `screener.yaml` si
preferís un universo más chico (escanea más rápido y consume menos cuota de
la API gratuita de datos).

- **Datos**: precios diarios via [`yfinance`](https://github.com/ranaroussi/yfinance)
  (Yahoo Finance no oficial, gratis, con límites de uso). Se cachean 15 min
  por símbolo para no agotar la cuota en cada refresh del dashboard. Durante
  un scan se espera `scan_request_delay_seconds` (0.15s por defecto) entre
  cada símbolo para no ráfagar la API con un universo grande — con el S&P 500
  completo, un scan en frío (sin cache) tarda varios minutos. Subí ese valor
  si ves errores de datos frecuentes; bajalo (0 está permitido) si usás un
  universo chico. Esta pausa se salta automáticamente para un símbolo que ya
  está cacheado (`is_bars_cached` en `app/market_data.py`): como las 4
  estrategias comparten `lookback_days`, escanear varias en el mismo ciclo
  (ej. `GET /api/signals/scan/all`) solo paga la pausa una vez por símbolo, no
  una vez por estrategia.
- **Señal**: combina momentum a 3 y 1 meses, fuerza relativa contra `SPY`,
  filtro de tendencia (precio > SMA20 > SMA50), RSI en una zona "sana" (ni
  sobrecomprado ni rompiendo a la baja) y un piso de liquidez en **dólares**
  (`min_avg_dollar_volume`: volumen promedio 20 días × precio, no cantidad de
  acciones — así una acción barata no pasa el filtro solo por moverse en
  volúmenes altos de acciones baratas). El stop-loss sugerido se calcula con
  ATR(14).
- **Ranking comparable entre símbolos**: el `score` final de cada estrategia
  (Momentum, Oportunista, Dividendos, Largo plazo) no suma directamente los
  valores crudos de sus componentes (que tienen escalas muy distintas entre
  sí, ej. RSI acotado 0-100 vs. un PE invertido sin techo): cada `scan()`
  convierte primero cada componente a su percentil (0-100) dentro del propio
  universo escaneado en ese ciclo, y solo después aplica los pesos
  configurados. Esto evita que un solo valor extremo en un componente (un PE
  absurdo, un retorno puntual atípico) dispare el score total de un símbolo
  por delante de candidatos parejos en todos los componentes. El score de
  `evaluate_symbol()` llamado de forma aislada (fuera de un `scan()`, ej. en
  tests) sigue siendo la suma cruda, ya que no hay un universo contra el cual
  calcular percentiles.
- **Filtro de régimen** (`regime_filter_enabled` para Momentum,
  `opportunistic_regime_filter_enabled` para Oportunista; comparten
  `regime_sma_period`, `regime_slope_lookback_days`,
  `regime_absolute_momentum_lookback_days`): no se sugieren entradas nuevas
  si el benchmark (`SPY` por defecto) no está en régimen alcista de fondo
  (pendiente positiva de su SMA de largo plazo + momentum absoluto positivo).
  Son flags independientes por estrategia: un re-test del backtest
  (2026-06-24) mostró que el filtro mejora a Momentum pero empeora a
  Oportunista (que compra giros/reversiones, justo lo que aparece cuando el
  mercado no está en tendencia alcista limpia) — por eso viene activado para
  Momentum y desactivado para Oportunista. Si no hay suficiente historia para
  calcularlo, el filtro no bloquea (asume régimen favorable en vez de fallar
  el scan por falta de dato).
- **Proximidad al máximo de 52 semanas** (`near_high_filter_enabled`,
  `max_pct_below_52w_high`): solo se consideran entradas en símbolos que
  cotizan a no más de ese % por debajo de su máximo de 52 semanas (15% por
  defecto). Favorece líderes cerca de máximos (breakouts) en vez de nombres ya
  extendidos a la baja — la cercanía al máximo de 52 semanas es un predictor
  de continuación de momentum bien documentado (George & Hwang, 2004). Si no
  hay historia suficiente para el máximo, no bloquea. Se aplica tanto en el
  scan en vivo como en el backtest.
- **Blackout de earnings** (`earnings_blackout_days`): no se sugieren
  entradas nuevas dentro de esa cantidad de días antes de la próxima fecha de
  earnings estimada (gap risk que el stop-loss basado en ATR no cubre). La
  fecha se obtiene de Yahoo Finance vía `yfinance`; si no se puede determinar,
  el filtro no bloquea (dato secundario, best-effort).
- **Trailing stop** (`trailing_stop_enabled`, apagado por defecto): ver
  "Salida" más abajo, dentro del motor de auto-trading.
- **Backtest** (`GET /api/signals/backtest`): corre la misma lógica de
  entrada/salida sobre la historia del universo configurado y devuelve
  métricas (win rate, profit factor, retorno acumulado vs. `SPY`, max
  drawdown, Sharpe ratio) a partir de una **curva de equity diaria real**: una
  posición abierta aporta su retorno no realizado todos los días que está
  abierta (no solo al cerrarse), usando el cierre real de mercado de cada día
  (no una interpolación entre la entrada y el resultado final), así que el
  drawdown combinado de operaciones solapadas en el tiempo queda reflejado
  con el camino de precio real. El `sharpe_ratio` se calcula sobre esos
  retornos diarios y se anualiza con `sqrt(252)`, igual que un Sharpe
  convencional; es `None` si hay menos de 2 operaciones. Modela comisión y
  slippage estimados (`commission_per_trade_usd`, `slippage_pct`) y un fill de
  stop-loss realista: el stop se chequea contra el **mínimo intradiario**, no
  el cierre, y si hubo un gap por debajo del stop el fill asumido es el precio
  de apertura (peor que el stop), no el cierre del día. Limita las posiciones
  abiertas a la vez a `top_n` (descarta las señales que no tendrían cupo
  libre, como en la operatoria real), y pondera cada operación como `1/top_n`
  del capital. Sigue siendo deliberadamente simple en otros aspectos — no
  modela el efecto del cash sin invertir cuando hay menos de `top_n`
  posiciones abiertas. Sirve para validar la dirección de la idea, no como
  promesa de resultados futuros.
- **Validación out-of-sample** (`GET /api/signals/backtest/walk-forward`):
  corre el mismo backtest **una sola vez** (no vuelve a pedir datos de
  mercado) y parte el período resultante en `n_folds` tramos consecutivos de
  igual duración calendario (3 por defecto, configurable entre 2 y 12),
  devolviendo las métricas resumen de cada tramo por separado. Sirve para
  detectar si el resultado del backtest completo está concentrado en un
  tramo de tiempo favorable puntual (ej. un solo mercado alcista) en vez de
  sostenerse a través de distintos períodos — algo que el resumen de todo el
  período de una sola vez no puede mostrar. **No es walk-forward
  optimization** en el sentido clásico: no hay re-ajuste de parámetros por
  ventana, porque esta herramienta no hace optimización de parámetros. Los
  thresholds configurados (RSI, SMAs, ATR, filtros de régimen/52 semanas,
  etc.) son siempre los mismos en todos los tramos — la pregunta que responde
  es si ese mismo set de reglas fijo se sostiene en distintos tramos de
  tiempo, no si existe una mejor combinación de parámetros.
- **Endpoints**: `GET /api/signals/scan` (lista rankeada, cacheada),
  `GET /api/signals/backtest`, `GET /api/signals/backtest/walk-forward`,
  `GET/PUT /api/signals/config` (el `PUT` requiere `X-API-Key`, igual que
  `/api/rules`).
- Edita `universe`, las ventanas de momentum/RSI/SMA, el multiplicador de ATR
  para el stop, etc. en `screener.yaml` (o vía `PUT /api/signals/config`).

Esto no es una recomendación de inversión ni un sistema que garantice ganarle
al mercado — es una herramienta de screening con una metodología transparente
que tú puedes auditar, ajustar y poner a prueba con el backtest antes de
arriesgar capital real.

### Escaneo proactivo y órdenes en borrador automáticas

Por defecto el radar de oportunidades es "pull": solo escanea cuando abres el
dashboard o pedís `GET /api/signals/scan`. Con `auto_scan_enabled: true` en
`screener.yaml` el backend además corre el mismo escáner solo, en background,
cada `auto_scan_interval_minutes` (30 por defecto):

- Detecta **transiciones**: un símbolo que antes no pasaba los filtros del
  screener y en este ciclo sí. No vuelve a avisar de un símbolo que ya viene
  pasando los filtros desde el ciclo anterior — solo de cambios de estado.
  El primer ciclo después de arrancar el backend (o después de cambiar
  `screener.yaml`) no genera avisos: solo establece la base de qué símbolos
  pasan, para no inundar la cola de pendientes con todo lo que ya venía
  pasando antes de que el backend arrancara.
- Por cada símbolo nuevo, arma una orden de **compra** en borrador (LMT al
  último precio, con el stop-loss sugerido por ATR), sizeada por riesgo con
  el mismo cálculo que `GET /api/orders/size-suggestion`
  (`RulesEngine.suggested_quantity`), y la pasa por
  `RulesEngine.evaluate()` — las mismas reglas que cualquier otra orden
  (whitelist, stop-loss, límites de tamaño, horario, kill switch, etc.). Si
  la rechaza alguna regla, no se crea el borrador.
- El borrador **siempre** queda en la cola de aprobación manual
  (`GET /api/orders/pending`), sin importar si su valor está por debajo de
  `manual_approval_threshold_usd`: una orden generada sin intervención
  humana nunca se ejecuta sola. Se distingue de las que armás vos a mano por
  el campo `source: "signal_engine"` (vs. `"user"`), y el dashboard la marca
  con 🤖.
- No arma un borrador si ya tenés una posición abierta en ese símbolo, o si
  ya hay una orden pendiente (de cualquier origen) para ese símbolo.
- **Tope por ciclo**: como mucho draftea `max_auto_drafts_per_cycle` (3 por
  defecto) órdenes en un solo ciclo, y nunca más allá de los cupos libres
  respecto a `top_n` contando lo que ya está pendiente. Si muchos símbolos
  pasan a la vez (ej. el régimen se vuelve alcista de golpe), se quedan los de
  mayor score; el resto no se draftea (errar hacia menos órdenes automáticas
  es el lado seguro).
- Mientras el trading está pausado (`halted`) o el backend no está conectado
  a IBKR, el ciclo no hace nada (ni siquiera escanea).
- Cada ciclo nuevo te avisa por el WebSocket (`type: "signal_alert"`, con
  `new_signals` y `drafted_orders`) y el dashboard muestra un toast con los
  símbolos detectados y cuántos borradores se crearon.

## Fondos (`backend/funds.json`)

Un fondo es una porción de capital con su propia contabilidad: cash, posiciones
y PnL realizado, llevados aparte de la cuenta consolidada de IBKR y de
cualquier otro fondo. Pensado para casos como "le doy a la herramienta $5.000
ficticios y quiero ver claramente cómo le va a esos $5.000", sin que se mezcle
con el resto de la cuenta (que en IBKR siempre se ve consolidada).

- **Mecánica de capital**: "darle" dinero a un fondo nunca mueve nada real en
  IBKR — el dinero real ya está depositado en la cuenta por fuera de esta
  herramienta. Asignarlo a un fondo es puramente un asiento contable interno
  (`Fund.capital_flows`): la creación de un fondo es, en este modelo, solo su
  primer aporte.
  - **Crear un fondo**: `POST /api/funds` con `{name, initial_capital_usd}`.
    `cash_usd` arranca igual a `initial_capital_usd` (registrado como el
    primer `CapitalFlow`) y se mueve con cada compra/venta atada a ese fondo
    (nunca se lee el cash real de IBKR para esto: es contabilidad puramente
    interna).
  - **Aportar o retirar capital después de creado**: `POST
    /api/funds/{id}/capital-flows` con `{amount, note?}` (`amount > 0` aporta,
    `amount < 0` retira). Un retiro no puede superar el `cash_usd` disponible
    del fondo (no se puede retirar plata que está en posiciones abiertas; hay
    que vender primero).
  - **Guardrail contra sobre-asignación**: cada aporte (al crear o después)
    se valida contra el cash real de la cuenta de IBKR
    (`broker.get_account_summary().cash`): la suma de `cash_usd` de *todos*
    los fondos nunca puede superar ese cash real. Sin este chequeo, la
    separación entre fondos sería una ilusión — un fondo podría "creer" que
    tiene plata que en realidad ya está asignada a otro fondo o no existe en
    la cuenta.
  - **PnL/ROI correctos al aportar o retirar plata**: el PnL se mide contra
    `Fund.net_contributed_capital()` (la suma de todos los `capital_flows`),
    no contra un capital inicial fijo — así, aportar o retirar plata más
    adelante no infla ni desinfla artificialmente el rendimiento. Es una
    medida "dollar-weighted" simple (no pondera por cuánto tiempo estuvo cada
    peso invertido, a diferencia de un *time-weighted return*, que podría
    agregarse más adelante si se necesita más rigor).
- **Atar una orden a un fondo**: `OrderRequest.fund_id` (opcional). Si se
  especifica, además de pasar por `RulesEngine.evaluate()` (igual que
  cualquier orden), se valida contra el fondo:
  - **Compra**: el fondo necesita `cash_usd` suficiente para el costo
    estimado.
  - **Venta**: la cantidad no puede superar lo que el *ledger* del fondo
    registra como propio de ese símbolo (`Fund.owned_quantity`). Esto es la
    capa de seguridad clave para no tocar holdings preexistentes en la
    cuenta de IBKR (o de otro fondo): un fondo que nunca compró un símbolo
    tiene cantidad registrada cero en ese símbolo, sin importar cuánto haya
    realmente en la cuenta — la venta se rechaza con 422 antes de llegar al
    broker.
- Tras una ejecución exitosa (inmediata o por aprobación manual), el fill se
  registra en el ledger del fondo (`Fund.record_fill`): actualiza `cash_usd`,
  el costo promedio de la posición y el PnL realizado en ventas.
  Simplificación conocida: como `broker.place_order()` hoy no espera ni
  devuelve el fill real de IBKR, se usa el precio de referencia/límite ya
  usado para validar la orden como aproximación del precio de fill (igual
  de transparente que las simplificaciones ya documentadas en
  `backtest.py`).
- **Toggle de auto-trading por fondo**: `PUT /api/funds/{id}/auto-trading`
  con `{enabled}`. Con `enabled: true`, ese fondo compra y vende dentro del
  escaneo proactivo **sin pasar por la cola de aprobación manual** (ver motor
  de auto-trading abajo). El escaneo proactivo en sí sigue armando borradores
  manuales sin `fund_id` para todo lo que no se asigne a un fondo
  auto-trading.

### Motor de auto-trading por fondo (solo modo paper, sin aprobación manual)

Con `Fund.auto_trading_enabled = true`, ese fondo opera de forma autónoma
dentro del mismo ciclo del escaneo proactivo (sección anterior), en vez de
quedar en un borrador a la espera de aprobación. Es **solo para modo
`paper`**: tanto la entrada como el monitor de salida chequean
`state["mode"] == "paper"` y no hacen nada si el backend está en `live`, sin
importar el toggle del fondo — un error de configuración nunca puede activar
trading autónomo con dinero real.

- **Entrada**: cuando una señal nueva pasa los filtros (la misma transición
  no-pasa → pasa de la sección anterior), antes de armar el borrador manual
  se intenta una entrada automática (`_try_auto_trade_entry`):
  - **Un solo fondo por señal**: si hay más de un fondo con auto-trading
    activado y cupo para comprar, la señal se asigna a un único fondo — el
    primero por orden de creación que no tenga ya posición en ese símbolo y
    pueda afrontar al menos 1 unidad. Evita que varios fondos compitan por el
    mismo símbolo a la vez y simplifica el monitoreo de salida.
  - El tamaño se calcula con `RulesEngine.suggested_quantity`, igual que el
    borrador manual, pero sizeado por riesgo contra el `equity_estimate()` de
    ESE fondo (no el equity de toda la cuenta de IBKR) para que un fondo
    chico no reciba una posición dimensionada como si tuviera detrás el
    capital de todos los demás fondos juntos. Además se recorta a lo que el
    `cash_usd` de ese fondo puede pagar.
  - La orden igual pasa por `RulesEngine.evaluate()` — las mismas reglas duras
    que cualquier otra orden (whitelist, stop-loss, límites de tamaño,
    horario, kill switch, etc.). Si la rechaza, no se ejecuta nada. A
    diferencia del borrador manual, aquí sí importa solo `decision.approved`
    (no `requires_manual_approval`): saltarse la aprobación manual es
    justamente el propósito del toggle.
  - Si se ejecuta, el fill se registra en el ledger del fondo
    (`FundsStore.record_fill`) con el `stop_loss_price` sugerido, que abre la
    posición (`FundPosition.opened_at` / `stop_loss_price`) para que el
    monitor de salida la pueda evaluar.
- **Salida**: un segundo loop en background (`_auto_exit_monitor_loop`, misma
  cadencia que el escaneo proactivo — las señales de salida se basan en
  cierres diarios, chequear más seguido no aporta nada) revisa, para cada
  posición abierta por auto-trading:
  - Primero, **trailing stop** (`trailing_stop_enabled`, apagado por
    defecto — cambia el perfil de riesgo de "stop fijo" a "stop que persigue
    el precio", así que es una decisión explícita del usuario, no el
    comportamiento por defecto): intenta subir (nunca bajar) el stop-loss ya
    colocado en IBKR, con la misma distancia en ATR que el stop inicial
    (`stop_loss_atr_multiplier`) pero recalculada sobre el ATR del chequeo
    actual: `nuevo_stop = último cierre - ATR_actual *
    stop_loss_atr_multiplier`. Solo actúa si la orden stop-loss original
    sigue viva en la sesión actual de este backend
    (`broker.modify_stop_price`, misma limitación de sesión que
    `get_trade_fill`: una reconexión pierde el rastro de la orden) y solo
    actualiza el `stop_loss_price` del ledger del fondo **después** de
    confirmar que IBKR aceptó el nuevo precio — el ledger nunca debe
    registrar un stop más favorable que el que de verdad protege la posición
    en el broker.
  - Después, evalúa tres motivos de cierre (en este orden):
  1. **Reconciliación de stop-loss**: toda compra con stop-loss
     (auto-trading o no) ya coloca en IBKR una orden bracket — padre + hijo
     `StopOrder` encadenado (ver `broker.place_order`) — así que el stop-loss
     en sí **ya se ejecuta solo del lado del broker**, sin que este backend
     tenga que vigilarlo. Lo que faltaba era reconciliar esa salida en el
     ledger del fondo: si la cantidad real en IBKR es menor a la que registra
     el fondo, se asume que el stop ya se disparó y se registra la venta
     (precio aproximado con el `stop_loss_price` guardado al abrir la
     posición — misma simplificación ya documentada para el resto del
     ledger).
  2. **`max_holding_days`**: igual regla que ya se simulaba en `backtest.py`,
     ahora aplicada en vivo sobre la fecha real de apertura
     (`FundPosition.opened_at`).
  3. **Ruptura de tendencia**: el cierre más reciente queda por debajo de la
     SMA rápida del screener (`sma_fast`), igual que en `backtest.py`.
  - Si corresponde cerrar, vende la posición completa con una orden MKT atada
    al fondo y registra el fill. Si no se puede obtener un precio de
    referencia, o IBKR rechaza la orden, la posición se deja abierta para
    reintentar en el próximo ciclo (no se fuerza una venta sin precio).
- Cada paso (entrada ejecutada/rechazada, trailing stop actualizado, salida
  por cada motivo, reconciliación) queda en el audit log
  (`auto_trade_executed`, `auto_trade_rejected`,
  `auto_trade_stop_loss_rejected`, `auto_trade_trailing_stop_updated`,
  `auto_trade_trailing_stop_check_failed`, `auto_trade_stop_loss_reconciled`,
  `auto_trade_exit`, `auto_trade_exit_failed`) y la salida además se avisa por
  WebSocket (`type: "auto_trade_exit"`).

## Instalación

### 1. Interactive Brokers

1. Instala **TWS** o **IB Gateway** y abre sesión en tu cuenta **paper
   trading** primero.
2. En *Configure → API → Settings*: habilita "Enable ActiveX and Socket
   Clients", anota el puerto (por defecto `7497` en TWS paper).
3. Deja TWS/IB Gateway corriendo — el backend se conecta a él, no lo reemplaza.

### 2. Backend

#### Opción rápida (Windows, sin terminal)

Doble click en `backend/start_windows.bat`. La primera vez instala todo solo
(crea el entorno, instala dependencias, genera un `.env` de prueba si no
existe) y después siempre abre el dashboard. La ventana negra que aparece
muestra:

- la dirección para abrir el dashboard desde la misma laptop, y
- un código QR + dirección para abrirlo desde tu tablet/celular conectado a
  la misma WiFi (sin tener que escribir IPs a mano).

Dejá esa ventana abierta mientras usás el dashboard; cerrarla lo apaga. El
`.env` de prueba que se crea automáticamente queda en modo `paper` y sin
`API_KEY` real — para conectar IBKR de verdad o pasar a `live`, editalo como
se explica en el resto de este README. `API_KEY` es además la contraseña que
te va a pedir la pantalla de login del dashboard.

#### Manual (cualquier sistema)

```bash
cd trading-dashboard/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edita .env: define API_KEY (va a ser tu contraseña de login), confirma IB_HOST/IB_PORT
uvicorn app.main:app --reload --port 8000
```

Abre `http://localhost:8000` — el dashboard se sirve desde el mismo backend.
Para acceder desde otro dispositivo en la misma red (ej. una tablet), corré
`uvicorn app.main:app --host 0.0.0.0 --port 8000` y usá la IP local de la
laptop en vez de `localhost`.

> ⚠️ **Correr siempre con un solo proceso worker** (no agregues `--workers N`
> a uvicorn ni uses gunicorn con varios workers). El estado en memoria
> (config del screener, órdenes pendientes, locks que serializan validación
> de fondos) vive en un solo proceso; con más de un worker, dos procesos
> podrían validar la misma orden contra el mismo saldo sin verse entre sí. Si
> necesitás más capacidad, escalá verticalmente (más CPU/RAM en la misma VM).

### 3. Tests

Los tests cubren el motor de reglas y los indicadores/screener/backtest
(lógica pura sobre datos sintéticos, sin necesitar IBKR ni internet):

```bash
cd trading-dashboard/backend
API_KEY=test-key python3 -m pytest tests/ -v
```

## Despliegue 24/7 en la nube (sin depender de la laptop)

Para que el motor (en paper o, más adelante, en live) siga corriendo sin
necesidad de dejar la laptop encendida, ver
[`deploy/README.md`](deploy/README.md): instala el backend y el login a
IBKR (vía [IBC](https://github.com/IbcAlpha/IBC)) en un servidor headless
— se recomienda una VM ARM "Always Free" de Oracle Cloud, gratis para
siempre — y lo expone solo a tus propios dispositivos mediante una red
privada de [Tailscale](https://tailscale.com) (gratis para uso personal),
sin abrir ningún puerto a internet.

## Pasar a cuenta real (`live`)

No lo hagas hasta haber probado el flujo completo en paper por un tiempo
razonable. El cambio tiene dos pasos: uno único en el servidor (habilitar que
este backend pueda operar en live) y, a partir de ahí, un botón en el
dashboard para alternar entre paper y live cuando quieras, sin reiniciar.

### Paso único: habilitar live en el `.env`

1. Define en `.env`: `LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY` y,
   si tu cuenta live usa un puerto distinto al de paper (lo normal),
   `IB_PORT_LIVE` con ese puerto (`7496` en TWS, `4001` en IB Gateway).
2. Revisa `rules.yaml` con números conservadores antes de arrancar.
3. Reinicia el backend una vez para que tome el nuevo `.env`.

Sin `LIVE_CONFIRM` definido, el botón de modo live del dashboard no hace
nada (devuelve un error explicando qué falta) — sigue siendo imposible
activar live por accidente con un solo click sin haber tocado el `.env`
antes, a propósito.

### Desde ahí: botón "Activar modo LIVE" en el dashboard

Una vez habilitado el paso anterior, el botón rojo del header alterna entre
paper y live reconectando a IBKR en el puerto correspondiente, sin reiniciar
el backend. Pide confirmación (un popup) antes de cambiar, y requiere tu
`X-API-Key`. Por seguridad, **cada cambio de modo deja el trading pausado**
(kill switch activado) — tenés que reanudarlo a mano desde "Pausar/Reanudar
trading" cuando quieras que vuelva a operar. El dashboard muestra un banner
rojo permanente mientras `mode=live`.

## Limitaciones conocidas

- El radar de oportunidades es un screener con metodología transparente, no
  una garantía de rendimiento — ninguna señal se ejecuta sola, sigue siendo
  el `RulesEngine` quien aprueba o rechaza cada orden.
- El precio de referencia para validar órdenes usa datos demorados (`delayed`)
  si no tienes suscripción de market data en tiempo real con IBKR.
- Los datos del screener/backtest vienen de Yahoo Finance via `yfinance`: no
  oficial, gratis, con límites de uso y sin SLA. Si falla o te quedas sin
  cuota, el dashboard lo informa en vez de inventar datos.
- El backtest es simplificado: modela comisión, slippage, un fill de
  stop-loss realista (mínimo intradiario, no el cierre), un tope de `top_n`
  posiciones concurrentes y una curva de equity diaria real (Sharpe estándar
  anualizado por `sqrt(252)`), pero no modela el efecto del cash ocioso cuando
  hay menos de `top_n` posiciones abiertas, ni la supervivencia histórica del
  universo (usa el universo configurado hoy, no el de la época analizada) —
  útil para validar la dirección de la idea, no para proyectar retornos.
- Pensado para uso personal/un solo usuario; no implementa multiusuario ni
  roles.
- Fondos: el precio de fill registrado en el ledger es el precio de
  referencia/límite usado para validar la orden, no el fill real reportado
  por IBKR (`place_order()` no lo espera todavía). Tampoco soportan reglas
  (`rules.yaml`) ni estrategia propias todavía — hoy comparten el mismo
  `RulesEngine` y el mismo screener que el resto de la cuenta.
- Motor de auto-trading: solo opera en modo `paper` (chequeo explícito, nunca
  en `live`). El monitor de salida corre cada `auto_scan_interval_minutes`
  (no en tiempo real): un trend-break o un `max_holding_days` puede tardar
  hasta ese intervalo en detectarse. La reconciliación de un stop-loss ya
  ejecutado por IBKR aproxima el precio de fill con el `stop_loss_price`
  registrado al abrir la posición, no con el fill real reportado por el
  broker (misma simplificación que el resto del ledger de fondos).
