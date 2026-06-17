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
│   ├── tests/test_rules.py
│   ├── rules.yaml        # límites de riesgo (editable)
│   └── .env.example
└── frontend/
    └── index.html        # dashboard (vanilla JS, sin build step)
```

El backend se conecta a **TWS o IB Gateway** (deben estar corriendo en algún
lugar accesible — tu laptop, una VM, etc.) usando `ib_async`. El frontend habla
solo con este backend (REST + WebSocket), nunca directo con IBKR.

## Modelo de seguridad

1. **Paper primero, siempre.** `TRADING_MODE` por defecto es `paper` y el
   puerto por defecto (`7497`) es el de TWS Paper Trading. Para pasar a `live`
   hay que definir además `LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY` en
   el `.env` — un solo flag no alcanza, es a propósito.
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

## Instalación

### 1. Interactive Brokers

1. Instala **TWS** o **IB Gateway** y abre sesión en tu cuenta **paper
   trading** primero.
2. En *Configure → API → Settings*: habilita "Enable ActiveX and Socket
   Clients", anota el puerto (por defecto `7497` en TWS paper).
3. Deja TWS/IB Gateway corriendo — el backend se conecta a él, no lo reemplaza.

### 2. Backend

```bash
cd trading-dashboard/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edita .env: define API_KEY, confirma IB_HOST/IB_PORT
uvicorn app.main:app --reload --port 8000
```

Abre `http://localhost:8000` — el dashboard se sirve desde el mismo backend.

### 3. Tests

Los tests cubren el motor de reglas (lógica pura, sin necesitar IBKR
conectado):

```bash
cd trading-dashboard/backend
API_KEY=test-key python3 -m pytest tests/ -v
```

## Pasar a cuenta real (`live`)

No lo hagas hasta haber probado el flujo completo en paper por un tiempo
razonable. Cuando estés listo:

1. Cambia `IB_PORT` al puerto de tu cuenta live en TWS/IB Gateway (`7496` o
   `4001`).
2. En `.env`: `TRADING_MODE=live` y
   `LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY`.
3. Revisa `rules.yaml` con números conservadores antes de arrancar.
4. El dashboard muestra un banner rojo permanente mientras `mode=live`.

## Limitaciones conocidas

- No incluye señales ni estrategias de trading — es un gateway de ejecución
  con reglas, tú decides qué orden enviar.
- El precio de referencia para validar órdenes usa datos demorados (`delayed`)
  si no tienes suscripción de market data en tiempo real con IBKR.
- Pensado para uso personal/un solo usuario; no implementa multiusuario ni
  roles.
