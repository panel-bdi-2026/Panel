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
  retornos diarios; es `None` si hay menos de 2 operaciones. Sigue siendo
  deliberadamente simple en otros aspectos — la curva de equity asume capital
  igualmente repartido entre operaciones de forma secuencial, no concurrencia
  real de posiciones. Sirve para validar la dirección de la idea, no como
  promesa de resultados futuros.
- **Endpoints**: `GET /api/signals/scan` (lista rankeada, cacheada),
  `GET /api/signals/backtest`, `GET/PUT /api/signals/config` (el `PUT`
  requiere `X-API-Key`, igual que `/api/rules`).
- Edita `universe`, las ventanas de momentum/RSI/SMA, el multiplicador de ATR
  para el stop, etc. en `screener.yaml` (o vía `PUT /api/signals/config`).

Esto no es una recomendación de inversión ni un sistema que garantice ganarle
al mercado — es una herramienta de screening con una metodología transparente
que tú puedes auditar, ajustar y poner a prueba con el backtest antes de
arriesgar capital real.

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
- El backtest es simplificado: aunque modela comisión, slippage y un fill de
  stop-loss realista (mínimo intradiario, no el cierre), no rastrea
  concurrencia real de posiciones (asume capital repartido secuencialmente
  entre operaciones) y su Sharpe ratio es una aproximación por operación, no
  el cálculo estándar sobre una curva de equity diaria — útil para validar la
  dirección de la idea, no para proyectar retornos.
- Pensado para uso personal/un solo usuario; no implementa multiusuario ni
  roles.
