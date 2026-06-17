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
3. **Lista blanca de símbolos vacía por defecto.** Sin excepción: si no agregas
   símbolos a `symbol_whitelist` en `rules.yaml`, no se puede operar nada.
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
   requieren `X-API-Key`** (enviar/aprobar/rechazar órdenes, cambiar
   `rules.yaml`, pausar/reanudar, y también leer cuenta/posiciones/auditoría —
   nada de eso es público en la red local). El WebSocket de actualizaciones en
   vivo pide la misma key como `?api_key=` en la URL, ya que el navegador no
   puede mandar headers personalizados en el handshake.
10. **El estado (`halted`, `mode`, órdenes pendientes) persiste en
    `state.json`** y sobrevive a un reinicio del backend — un reinicio no
    vuelve a dejar el trading activo silenciosamente si lo habías pausado.
11. **El escaneo proactivo nunca ejecuta nada por sí solo.** Si lo habilitás
    (`auto_scan_enabled` en `screener.yaml`), detecta señales nuevas en
    background y arma órdenes de compra en *borrador* — pasan por el mismo
    `RulesEngine` que cualquier orden y siempre quedan en la cola de
    aprobación manual, sin importar su valor estimado. Ver "Escaneo proactivo
    y órdenes en borrador automáticas" más abajo.
12. **Símbolos validados estrictamente.** El símbolo de toda orden se valida
    contra `^[A-Z0-9.\-]{1,12}$` antes de guardarse o mostrarse, y el frontend
    escapa todo texto dinámico antes de renderizarlo. Sin esto, un "símbolo"
    con HTML/JavaScript podía quedar persistido y ejecutarse en el navegador
    de quien abriera el dashboard (XSS almacenado capaz de robar la API key).
13. **Superficie de red mínima.** Escanear el mercado y correr el backtest
    también requieren `X-API-Key` (no mueven dinero, pero consumen la cuota de
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

Ninguna de estas reglas reemplaza tu propio criterio. Esto no es una
recomendación de inversión ni una garantía de que una orden "aprobada" sea una
buena idea — solo que respeta los límites que tú configuraste.

## Reglas configurables (`backend/rules.yaml`)

| Campo | Qué hace |
|---|---|
| `symbol_whitelist` | Símbolos permitidos. Vacío = nada permitido. |
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
sugerir algo que el `RulesEngine` rechazaría de todas formas. Es una
sugerencia editable: no se aplica sola ni se envía ninguna orden por esto.

## Radar de oportunidades (`backend/screener.yaml`)

Capa opcional de análisis que **rankea candidatos de un universo de acciones
por una estrategia momentum/técnica** (tendencia + fuerza relativa vs. el
mercado + RSI + liquidez), pensada para swing trading (días-semanas) en
acciones de EEUU. No ejecuta nada por su cuenta: solo sugiere. Cualquier orden
que decidas enviar a partir de un candidato pasa exactamente por el mismo
`RulesEngine` que cualquier otra orden — whitelist, stop-loss, límites de
tamaño, todo aplica igual.

- **Datos**: precios diarios via [`yfinance`](https://github.com/ranaroussi/yfinance)
  (Yahoo Finance no oficial, gratis, con límites de uso). Se cachean 15 min
  por símbolo para no agotar la cuota en cada refresh del dashboard.
- **Señal**: combina momentum a 3 y 1 meses, fuerza relativa contra `SPY`,
  filtro de tendencia (precio > SMA20 > SMA50), RSI en una zona "sana" (ni
  sobrecomprado ni rompiendo a la baja) y un piso de liquidez en **dólares**
  (`min_avg_dollar_volume`: volumen promedio 20 días × precio, no cantidad de
  acciones — así una acción barata no pasa el filtro solo por moverse en
  volúmenes altos de acciones baratas). El stop-loss sugerido se calcula con
  ATR(14).
- **Filtro de régimen** (`regime_filter_enabled`, `regime_sma_period`): no se
  sugieren entradas nuevas si el benchmark (`SPY` por defecto) está por
  debajo de su propia SMA de largo plazo (200 días por defecto) — evita
  proponer compras "momentum" cuando el mercado de fondo está en tendencia
  bajista. Si no hay suficiente historia para calcular la SMA, el filtro no
  bloquea (asume régimen favorable en vez de fallar el scan por falta de
  dato).
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
- **Backtest** (`GET /api/signals/backtest`): corre la misma lógica de
  entrada/salida sobre la historia del universo configurado y devuelve
  métricas (win rate, profit factor, retorno acumulado vs. `SPY`, max
  drawdown, Sharpe ratio aproximado). Modela comisión y slippage estimados
  (`commission_per_trade_usd`, `slippage_pct`) y un fill de stop-loss
  realista: el stop se chequea contra el **mínimo intradiario**, no el
  cierre, y si hubo un gap por debajo del stop el fill asumido es el precio
  de apertura (peor que el stop), no el cierre del día. El `sharpe_ratio` se
  aproxima a partir de los retornos por operación (no de una curva de equity
  diaria), así que no es comparable 1:1 con un Sharpe calculado sobre
  retornos diarios; es `None` si hay menos de 2 operaciones. Limita las
  posiciones abiertas a la vez a `top_n` (descarta las señales que no
  tendrían cupo libre, como en la operatoria real), y pondera cada operación
  como `1/top_n` del capital. Sigue siendo deliberadamente simple en otros
  aspectos — no modela el efecto del cash sin invertir cuando hay menos de
  `top_n` posiciones abiertas. Sirve para validar la dirección de la idea, no
  como promesa de resultados futuros.
- **Endpoints**: `GET /api/signals/scan` (lista rankeada, cacheada),
  `GET /api/signals/backtest`, `GET/PUT /api/signals/config` (el `PUT`
  requiere `X-API-Key`, igual que `/api/rules`).
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
  con `{enabled}`. Por ahora el campo solo se persiste — ningún motor lo lee
  todavía para operar sin aprobación manual. Existe desde ya para que el
  toggle esté disponible en el dashboard de cara a la fase donde el motor
  proactivo pueda ejecutar compras/ventas de forma autónoma dentro de un
  fondo específico (ver discusión de roadmap; no implementado en esta
  versión).
- El escaneo proactivo (sección anterior) sigue siendo independiente de los
  fondos por ahora: sus borradores no quedan atados a ningún `fund_id`.

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
se explica en el resto de este README.

#### Manual (cualquier sistema)

```bash
cd trading-dashboard/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edita .env: define API_KEY, confirma IB_HOST/IB_PORT
uvicorn app.main:app --reload --port 8000
```

Abre `http://localhost:8000` — el dashboard se sirve desde el mismo backend.
Para acceder desde otro dispositivo en la misma red (ej. una tablet), corré
`uvicorn app.main:app --host 0.0.0.0 --port 8000` y usá la IP local de la
laptop en vez de `localhost`.

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
  stop-loss realista (mínimo intradiario, no el cierre) y un tope de `top_n`
  posiciones concurrentes, pero no modela el efecto del cash ocioso cuando hay
  menos de `top_n` posiciones abiertas, y su Sharpe ratio es una aproximación
  por operación, no el cálculo estándar sobre una curva de equity diaria —
  útil para validar la dirección de la idea, no para proyectar retornos.
- Pensado para uso personal/un solo usuario; no implementa multiusuario ni
  roles.
- Fondos: el precio de fill registrado en el ledger es el precio de
  referencia/límite usado para validar la orden, no el fill real reportado
  por IBKR (`place_order()` no lo espera todavía). Tampoco soportan reglas
  (`rules.yaml`) ni estrategia propias todavía — hoy comparten el mismo
  `RulesEngine` y el mismo screener que el resto de la cuenta; el toggle de
  auto-trading por fondo se persiste pero ningún motor lo lee aún.
