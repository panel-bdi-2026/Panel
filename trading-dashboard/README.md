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
   alcanza `daily_loss_limit_pct`.
6. **Stop-loss obligatorio en compras** (configurable, pero viene activo por
   defecto) y con un tope de riesgo (`max_stop_loss_pct`) para que nadie meta
   un stop tan lejano que no proteja nada.
7. **Auditoría inmutable.** Cada intento de orden (aprobada, rechazada,
   pendiente, ejecutada) queda en `audit.db` con timestamp y el resultado
   completo de la evaluación de reglas.
8. **Endpoints que mueven dinero o cambian reglas requieren `X-API-Key`**
   (enviar/aprobar/rechazar órdenes, cambiar `rules.yaml`, pausar/reanudar).

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
| `manual_approval_threshold_usd` | Por encima de este monto, la orden queda pendiente de tu aprobación. |
| `allow_extended_hours` / `trading_hours_*` | Restringe operar a horario regular de mercado. |

Puedes editar el archivo directamente (requiere reiniciar el backend) o vía
`PUT /api/rules` con tu `X-API-Key`.

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
  sobrecomprado ni rompiendo a la baja) y un piso de liquidez. El stop-loss
  sugerido se calcula con ATR(14).
- **Backtest** (`GET /api/signals/backtest`): corre la misma lógica de
  entrada/salida sobre la historia del universo configurado y devuelve
  métricas (win rate, profit factor, retorno acumulado vs. `SPY`, max
  drawdown). Es deliberadamente simple — no modela comisiones ni slippage, y
  la curva de equity asume capital igualmente repartido entre operaciones de
  forma secuencial, no concurrencia real. Sirve para validar la dirección de
  la idea, no como promesa de resultados futuros.
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
- El backtest es simplificado (sin comisiones/slippage, sin concurrencia real
  de posiciones) — útil para validar la dirección de la idea, no para
  proyectar retornos.
- Pensado para uso personal/un solo usuario; no implementa multiusuario ni
  roles.
