# Roadmap: trading-dashboard hacia producción

Documento vivo. Actualizar a medida que se completan fases o cambian prioridades.
Última revisión: 2026-07-04.

---

## Estado actual (baseline de referencia)

| Dimensión                        | Calificación | Bloqueante principal                              |
|----------------------------------|:------------:|---------------------------------------------------|
| Testing y calidad del código     | 8/10         | main.py monolítico (3.305 líneas)                 |
| Gestión de riesgo                | 7/10         | Sin VaR/Kelly; trailing stop apagado              |
| Fundamento teórico de estrategias| 7/10         | Solo 3 años de backtest, bull market              |
| Arquitectura del backend         | 5/10         | main.py monolítico; estado en RAM                 |
| Metodología del backtest         | 5/10         | 3 años, look-ahead bias, sin walk-forward         |
| Seguridad                        | 5/10         | Sin rate limiting, JWT, ni HTTPS                  |
| UI/UX                            | 4/10         | Vanilla JS single-file, sin framework             |
| Probabilidad de generar retorno  | 4/10         | Backtest solo 3 años de bull market; sin walk-forward |
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

### 1.2 Backtest extendido a 15 años + walk-forward *(próximo paso)*

**Por qué:** el período 2023-2026 incluye uno de los rallies más fuertes en décadas.
No se sabe qué hacen estas estrategias en 2008, 2011, 2015-16, 2018 Q4, 2020 COVID, 2022 bear.

**Plan:**
1. Con Tiingo (disponible), descargar historia desde 2010 (o antes según disponibilidad)
2. Re-correr backtest de Momentum y Oportunista sobre el período completo
3. Implementar walk-forward validation:
   - Optimizar parámetros sobre 2010-2020 (10 años de training)
   - Validar sobre 2020-2025 out-of-sample (sin tocar)
   - Si los parámetros del training funcionan en el test → evidencia real de robustez
4. Calcular métricas por sub-período: bull (2010-15, 2016-21), bear (2011, 2015-16, 2022),
   crisis (2020 COVID). Publicar resultados en tabla.
5. Monte Carlo: randomizar el orden de los 700+ trades 10.000 veces.
   Rango del percentil 5-95 de retornos → ¿cuánto del resultado es skill vs suerte de secuencia?

**Esfuerzo estimado con Claude:** 1 semana (más tiempo de cómputo de los runs).

**Señal de éxito:** Momentum y Oportunista muestran Sharpe > 0.5 y DD < -30% en el backtest
completo 15 años, incluyendo 2022. Si no pasan ese test, hay que re-optimizar con el período largo.

---

### 1.3 CI/CD + backups + infraestructura básica

**Por qué:** el deploy hoy es manual, el sudoers de claude-rc no existe, y no hay backups
automáticos. Un crash sin backup borra el estado de todos los fondos.

**Plan:**
1. **Arreglar sudoers** (5 minutos): como root, crear `/etc/sudoers.d/claude-rc` con:
   ```
   claude-rc ALL=(root) NOPASSWD: /bin/systemctl restart trading-dashboard
   claude-rc ALL=(root) NOPASSWD: /bin/journalctl
   ```
2. **Backups automáticos**: cron job diario que copia `funds.json`, `screener.yaml`,
   `rules.yaml`, `audit.db` a un directorio con retención de 30 días (local o S3/B2).
3. **GitHub Actions CI/CD**:
   - En cada push: correr los 677 tests automáticamente
   - En merge a la rama de trabajo: deploy automático al droplet vía SSH
   - Esto hace que "haz el deploy" sea un `git merge`
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

**Fase 1.1 — hecho:**
- [x] Migrar `market_data.py` de yfinance a Tiingo (EOD ajustado, rate limiter 2 req/s)
- [x] Confirmar en producción: 200 OK en todos los tickers, sin 429

**Fase 1.1 — pendiente de validación:**
- [ ] Comparar precios Tiingo vs yfinance en 50 tickers
- [ ] Re-correr backtest de Momentum y Oportunista con datos Tiingo; documentar diferencias

**Fase 1.2 — siguiente:**
- [ ] Extender backtest a 2010-2025 con Tiingo (15 años)
- [ ] Implementar walk-forward validation (train 2010-2020, test 2020-2025)
- [ ] Métricas por sub-período: bear 2011/2015/2022, crisis 2020
- [ ] Monte Carlo de retornos (10.000 permutaciones)

**Fase 1.3 — en paralelo:**
- [ ] Arreglar sudoers de claude-rc como root (5 minutos)
- [ ] Implementar backups automáticos de `funds.json`, `screener.yaml`, `audit.db`
- [ ] GitHub Actions CI/CD: tests en cada push, deploy en merge
- [ ] Health check endpoint + alertas Telegram/email

---

## Decisiones registradas

| Fecha      | Decisión                                                        | Resultado |
|------------|-----------------------------------------------------------------|-----------|
| 2026-07-01 | Oportunista: stop 2.5x + rsi_max=65 + holding=20               | Backtest: +199%, Sharpe=1.42, DSR=100% |
| 2026-07-01 | Momentum: near_high=20% + holding=25d                          | Backtest: +63%, Sharpe=0.89, DSR=94%  |
| 2026-06-23 | Oportunista: regime filter apagado                              | El filtro empeoraba retorno acumulado |
| 2026-06-22 | MACD crossover gate activado por defecto                        | Sharpe +19%, DD -11pp                 |
| 2026-07-04 | Migrar datos históricos a Tiingo Power ($30/mes) en vez de Polygon | Sin 429 en producción, 2 req/s sostenidos |
