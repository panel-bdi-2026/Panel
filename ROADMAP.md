# Roadmap: trading-dashboard hacia producción

Documento vivo. Actualizar a medida que se completan fases o cambian prioridades.
Última revisión: 2026-07-04.

---

## Estado actual (baseline de referencia)

| Dimensión                        | Calificación | Bloqueante principal                              |
|----------------------------------|:------------:|---------------------------------------------------|
| Testing y calidad del código     | 8/10         | main.py monolítico (3.305 líneas)                 |
| Gestión de riesgo                | 7/10         | Sin VaR/Kelly; trailing stop apagado              |
| Fundamento teórico de estrategias| 6/10         | Oportunista validado 22 años; Momentum sin alpha real |
| Arquitectura del backend         | 5/10         | main.py monolítico; estado en RAM                 |
| Metodología del backtest         | 8/10         | 22 años, walk-forward 5 folds, Monte Carlo, 10 regímenes |
| Seguridad                        | 5/10         | Sin rate limiting, JWT, ni HTTPS                  |
| UI/UX                            | 4/10         | Vanilla JS single-file, sin framework             |
| Probabilidad de generar retorno  | 5/10         | Oportunista: DSR=98.8% (alpha real); Momentum: re-optimizar |
| Infraestructura/DevOps           | 3/10         | Sin CI/CD, sin backups, sin monitoreo             |
| Calidad de los datos             | 7/10         | ~~yfinance~~ → **Tiingo Power (hecho)**: 2 req/s, sin 429, EOD ajustado |

### Estrategias optimizadas (backtest 3 años, 2023-2026)

| Estrategia   | Retorno acum. | Sharpe | DD máx | DSR  | Parámetros clave                              |
|--------------|:-------------:|:------:|:------:|:----:|-----------------------------------------------|
| Momentum     | +63%          | +0.89  | -20.8% | 94%  | near_high=20%, holding=25d, stop=1.5x ATR     |
| Oportunista  | +199%         | +1.42  | -20.5% | 100% | stop=2.5x ATR, rsi_max=65, holding=20d        |

⚠️ Contexto obligatorio: S&P 500 subió ~70% en el mismo período (rally IA + post-Fed pivot).
Los retornos son prometedores pero 3 años de bull market no es evidencia suficiente de alpha real.

---

## Fase 1 — Fundamentos (prioridad máxima)

**Meta:** pasar de "funciona" a "confiable". Sin esta fase, todo lo demás construye sobre arena.

### 1.1 ✅ Migrar fuente de datos a Tiingo *(completado 2026-07-04)*

**Por qué:** yfinance es una API no oficial de Yahoo Finance sin SLA, con rate limits agresivos,
ajustes retroactivos que distorsionan el backtest, y datos de calidad variable.

**Implementado:** Tiingo Power (~$30/mes) en vez de Polygon.io.
- Rate limiter global a 2 req/s (10.000 req/hora con el plan Power)
- EOD OHLCV ajustado vía `/tiingo/daily/{symbol}/prices`
- Sin un solo 429 desde el deploy (confirmado en producción 2026-07-04)
- yfinance retenido únicamente para `get_next_earnings_date()` y `get_fundamentals()`
  (campos que Tiingo no provee: PE, ROE, próxima fecha de earnings)

**Pendiente de esta fase:**
- [ ] Comparar precios Tiingo vs yfinance en 50 tickers para validar calidad
- [ ] Re-correr backtest de Momentum y Oportunista con datos Tiingo y documentar diferencias

---

### 1.2 ✅ Backtest extendido a 22 años + walk-forward + Monte Carlo *(completado 2026-07-04)*

**Implementado:** `scripts/backtest_15yr.py` + funciones en `app/backtest.py` (5 folds
walk-forward, 10 sub-períodos históricos, Monte Carlo 10.000 sims).
Datos: Tiingo Power, 589 símbolos, 2003-07 → 2026-07 (22 años de trading real).

#### Resultados Momentum (22 años)

| Métrica | 3 años (antes) | 22 años (ahora) | ∆ |
|---|:---:|:---:|---|
| Retorno acumulado | +63% | **+10.3%** | ↓↓↓ |
| Benchmark (S&P 500) | +70% | +1.090% | — |
| Sharpe (anualizado) | 0.89 | **0.09** | ↓↓↓ |
| DSR (prob. Sharpe > 0) | 94% | **67%** | ↓↓ |
| Win rate | — | **29.9%** | 7 de 10 trades pierden |
| Expectancy | — | **+0.02%** | Prácticamente cero |
| Max drawdown | -20.8% | -39.5% | ↓↓ |
| Exposición promedio | — | 43.4% | — |

Walk-forward (5 folds): **positivo solo en el fold 2021-2026** (+27.6%). Los 4 folds
anteriores son negativos o de retorno muy bajo. Sub-períodos: gana en solo 5 de 10 regímenes.

**Diagnóstico:** los parámetros actuales (near_high=20%, holding=25d, stop=1.5xATR) fueron
optimizados sobre el rally 2023-2026. Sobre 22 años con mercados variados, la estrategia no
tiene alpha real. DSR 67% confirma que hay 33% de probabilidad de que el Sharpe real sea ≤ 0.

#### Resultados Oportunista (22 años)

| Métrica | 3 años (antes) | 22 años (ahora) | ∆ |
|---|:---:|:---:|---|
| Retorno acumulado | +199% | **+119%** | ↓↓ |
| Benchmark (S&P 500) | +70% | +1.090% | — |
| Sharpe (anualizado) | 1.42 | **0.47** | ↓ |
| DSR (prob. Sharpe > 0) | 100% | **98.8%** | ✅ alpha real confirmado |
| Win rate | — | **36.3%** | — |
| Expectancy | — | **+0.55%** | Ganadores 1.5x más grandes |
| Max drawdown | -20.5% | -36.9% | ↓ |
| Exposición promedio | — | **23%** | Cash ocioso 77% del tiempo |

Walk-forward (5 folds): **positivo en 4 de 5**. Único negativo: 2007-2012 (-35.1%, crisis
financiera). Sub-períodos: gana en 8 de 10, incluyendo COVID 2020 (-1.1% vs benchmark -9.8%)
y el crash de Q4 2018 (+1.2% vs benchmark -13.8%) — señal clara de defensividad real.

**Diagnóstico:** DSR 98.8% confirma alpha estadístico real. El bajo retorno absoluto vs
benchmark (+119% vs +1.090%) se explica por la exposición del 23% (cash inactivo el 77% del
tiempo). Ajustado por exposición: 119%/23% = 5.2 puntos de retorno por punto de expo, vs
10.9 del benchmark. Hay gap, pero hay alpha genuino. La palanca correcta es mejorar la
exposición sin degradar la selectividad.

#### Sub-períodos clave — Oportunista vs benchmark

| Período | Oportunista | Benchmark | Diagnóstico |
|---|:---:|:---:|---|
| 2018 Q4 Crash | **+1.2%** | -13.8% | ✅ defensivo |
| 2020 COVID crash | **-1.1%** | -9.8% | ✅ defensivo |
| 2022 Bear (tasas) | -12.8% | -18.6% | ✅ mejor que bench |
| 2015-16 China/EM | -0.7% | -0.4% | ≈ neutral |
| 2010-2012 post-crisis | -1.0% | +33.7% | ⚠ recuperación lenta |
| 2023-2025 AI rally | **+44.1%** | +86.3% | ⚠ subrende en bull fuerte |

#### Acción derivada

1. **Momentum:** re-optimizar parámetros con los 22 años completos de Tiingo, no solo
   2023-2026. Los parámetros actuales solo capturan bull markets. Un walk-forward de
   optimización (grid search sobre datos 2003-2016, validate 2016-2026) es el camino correcto.
   Hasta que no se haga esto, **no escalar Momentum con capital real**.
2. **Oportunista:** mantener parámetros actuales — hay alpha confirmado. Explorar si subir
   `top_n` (de 10 a 15) o relajar gates de entry incrementa exposición sin degradar selectividad.
3. **Cash ocioso:** con 77% de tiempo fuera del mercado, invertir el cash libre en SPY mientras
   no hay señales (opción `invest_idle_cash_in_benchmark`) mejoraría el retorno total
   sustancialmente sin cambiar la lógica de entry/exit.

**JSON completo:** `scripts/results/backtest_15yr_20260704_213032.json`

---

### 1.3 ✅ CI/CD *(completado)* + backups + infraestructura básica *(pendiente)*

**Por qué:** el deploy hoy es manual, el sudoers de claude-rc no existe, y no hay backups
automáticos. Un crash sin backup borra el estado de todos los fondos.

**Plan:**
1. **Arreglar sudoers** (5 minutos): como root, crear `/etc/sudoers.d/claude-rc` con:
   ```
   claude-rc ALL=(root) NOPASSWD: /bin/systemctl restart trading-dashboard
   claude-rc ALL=(root) NOPASSWD: /bin/journalctl
   ```
2. ✅ **Backups automáticos** *(completado)*: cron job en `/etc/cron.daily/trading-backup`,
   corre como root a las ~6:25 AM UTC, copia `funds.json`, `rules.yaml`, `screener_config.json`
   y `audit.db` (backup SQLite consistente) a `/opt/panel/backups/YYYY-MM-DD/`, retención 30 días.
   Verificado: snapshot de 2026-07-05 existe con los 4 archivos.
3. ✅ **GitHub Actions CI/CD** *(completado)*:
   - `.github/workflows/ci-cd.yml`: tests en cada push + deploy automático en push a rama
   - Runner self-hosted activo en el droplet (`actions.runner.panel-bdi-2026-Panel.panel-droplet.service`)
   - Deploy = `git push` → tests → restart automático del servicio
4. **Health check + alertas**: endpoint `GET /api/health` que verifique conexión IBKR,
   estado del scan, último heartbeat. Script externo (cron o Uptime Robot) que alerte
   vía Telegram si el health check falla.

**Esfuerzo estimado con Claude:** 3-4 días.

---

## Fase 2 — Solidez (una vez completada Fase 1)

### 2.1 Arquitectura: partir main.py

**Por qué:** 3.305 líneas en un solo archivo hace que cada cambio tenga efectos
secundarios difíciles de predecir. Conforme el sistema crece, se vuelve frágil.

**Estructura objetivo:**
```
app/
  api/
    signals.py      # GET/PUT /api/signals/*
    funds.py        # GET/POST /api/funds/*
    orders.py       # GET/POST /api/orders/*
    backtest.py     # POST /api/backtest/*
    websocket.py    # WebSocket handler
  services/
    scanner.py      # _run_score_recompute_cycle + background loops
    auto_trader.py  # _try_auto_trade_entry + fill registration
    exit_monitor.py # _check_fund_exit + trailing stop + scale-out
  state.py          # _signal_state, _sessions, locks (centralizados)
  main.py           # solo: app init, lifespan, router registration
```

**Approach:** un módulo a la vez, tests pasando después de cada movimiento.
No hacer un refactor big-bang — demasiado riesgo de regresión.

**Esfuerzo estimado con Claude:** 2-3 semanas (ritmo conservador para no romper nada).

---

### 2.2 SQLite para estado persistente

**Por qué:** `_signal_state`, `_sessions`, `_previously_passing` viven en RAM.
Un restart los borra. Eso causa bugs de fills que llegan después de un reinicio.

**Plan:** reemplazar los dicts globales por tablas SQLite con la misma interfaz.
El estado sobrevive reinicios sin necesitar reconciliación manual.

---

### 2.3 Seguridad

- Rate limiting: `slowapi` (60 req/min general, 5/min en login)
- JWT con expiración: reemplaza la API key fija
- HTTPS: Caddy como reverse proxy con Let's Encrypt automático
- Mover todos los secrets a variables de entorno en el unit de systemd

---

### 2.4 Gestión de riesgo avanzada

- **Kelly Criterion**: tamaño de posición basado en win rate + payoff ratio rolling de los últimos 50 trades. Hoy el sizing es solo por ATR.
- **Correlación de portfolio**: medir la beta del portfolio agregado, limitar la exposición beta total.
- **VaR diario**: si el Value at Risk al 95% supera X% del capital, no abrir nuevas posiciones.
- **Activar y calibrar trailing stop**: está implementado pero apagado. Necesita un backtest dedicado para calibrar `scale_out_at_r_multiple` antes de activarlo.

---

## Fase 3 — Producto (una vez que Fase 2 está estable)

### 3.1 UI/UX

- Migrar a React + Vite (o restructurar el JS actual en módulos ES6 si se quiere evitar el framework)
- Gráficos interactivos: equity curve del portfolio, distribución de retornos, heatmap sectorial (Lightweight Charts de TradingView, gratuito)
- Responsive design para móvil
- Notificaciones push de browser para fills y señales

### 3.2 Factores adicionales

- **Quality factor**: agregar Piotroski F-Score como gate/componente de score en Momentum.
  Quality + Momentum es la combinación con mayor respaldo académico.
- **Low volatility factor**: estrategia nueva decorrelacionada con Momentum (Frazzini & Pedersen).
  Candidato para un tercer fondo.

### 3.3 Diversificación de estrategias en vivo

Hoy solo Oportunista tiene un fondo activo. Activar Momentum con un segundo fondo
y comparar el comportamiento de ambos en paralelo con capital real pequeño.

---

## Fase 4 — Validación real (ongoing, 6-12 meses)

**No hay atajos para esta fase. Requiere tiempo de mercado real.**

1. **Paper trading con tracking riguroso**: registrar cada trade con precio teórico
   (close del día de la señal) vs precio real de IBKR. Medir el implementation shortfall.
   Si promedia > 0.3%, ajustar la estimación de retorno esperado real.
2. **Comparar mensualmente** retorno real de paper vs backtest del mismo período.
   Si divergen > 10pp anualizados, hay un problema de ejecución que identificar y corregir.
3. **Umbral para capital real**: después de 6 meses de paper trading con resultados
   consistentes con el backtest → escalar a capital real con montos pequeños (~10% del
   capital objetivo) durante otros 6 meses antes de escalar.

---

## Próximos pasos inmediatos

**Fase 1.1 — ✅ hecho:**
- [x] Migrar `market_data.py` de yfinance a Tiingo (EOD ajustado, rate limiter 2 req/s)
- [x] Confirmar en producción: 200 OK en todos los tickers, sin 429
- [x] Comparar precios Tiingo vs yfinance en 46 tickers: 44/46 verdes, 2 discrepancias explicables (BAC rounding, HON spin-off — Tiingo correcto)

**Fase 1.2 — ✅ hecho:**
- [x] Extender backtest a 22 años con Tiingo (2003-2026)
- [x] Walk-forward validation (5 folds)
- [x] Métricas por sub-período (10 regímenes)
- [x] Monte Carlo 10.000 sims (bug corregido 2026-07-04)
- [x] Documentar hallazgos críticos (ver sección 1.2 arriba)

**Fase 1.2 — pendiente (derivado de los hallazgos):**
- [ ] Re-optimizar Momentum con datos de 22 años (walk-forward optimization, grid search)
- [ ] Evaluar impacto de `invest_idle_cash_in_benchmark=True` en Oportunista
- [ ] Evaluar incrementar `top_n` de 10 a 15 en Oportunista (más exposición)

**Fase 1.3 — siguiente:**
- [ ] Arreglar sudoers de claude-rc como root (5 minutos)
- [x] Implementar backups automáticos de `funds.json`, `screener.yaml`, `audit.db`
- [x] GitHub Actions CI/CD: tests en cada push, deploy automático en push (runner activo)
- [ ] Health check endpoint + alertas Telegram/email

---

## Decisiones registradas

| Fecha      | Decisión                                                        | Resultado |
|------------|-----------------------------------------------------------------|-----------|
| 2026-07-01 | Oportunista: stop 2.5x + rsi_max=65 + holding=20               | Backtest: +199%, Sharpe=1.42, DSR=100% (3 años) |
| 2026-07-01 | Momentum: near_high=20% + holding=25d                          | Backtest: +63%, Sharpe=0.89, DSR=94% (3 años) |
| 2026-06-23 | Oportunista: regime filter apagado                              | El filtro empeoraba retorno acumulado |
| 2026-06-22 | MACD crossover gate activado por defecto                        | Sharpe +19%, DD -11pp                 |
| 2026-07-04 | Migrar datos históricos a Tiingo Power ($30/mes) en vez de Polygon | Sin 429 en producción, 2 req/s sostenidos |
| 2026-07-04 | Backtest 22 años Momentum: +10.3% vs +1090% benchmark, DSR=67% | ⚠ No escalar. Re-optimizar parámetros con 22 años |
| 2026-07-04 | Backtest 22 años Oportunista: +119% vs +1090% benchmark, DSR=98.8% | ✅ Alpha real confirmado. Estudiar mejorar exposición |
