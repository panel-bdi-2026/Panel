from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# Tickers reales son letras, digitos, punto y guion (ej. BRK.B, RDS-A). Se
# valida estrictamente el simbolo de toda orden entrante: sin esto, el campo
# aceptaba cualquier texto (solo se hacia strip().upper()), que despues se
# persistia en audit.db / ordenes pendientes y se renderizaba en el dashboard.
# Un "simbolo" tipo '<img src=x onerror=...>' quedaba como XSS almacenado
# capaz de robar la API key del localStorage de quien abriera el panel.
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")


def validate_symbol(value: str) -> str:
    """Normaliza y valida un ticker contra _SYMBOL_RE. Punto de entrada
    compartido para cualquier input de simbolo (ordenes, universo del
    screener, query params), no solo OrderRequest: cualquier otro lugar
    que reciba un simbolo en texto libre y lo persista o lo use para
    construir una whitelist necesita la misma sanitizacion, o queda como
    una puerta de entrada sin el chequeo que protege contra XSS almacenado
    e inyeccion de datos malformados."""
    v = value.strip().upper()
    if not _SYMBOL_RE.match(v):
        raise ValueError(
            "Simbolo invalido: solo se permiten letras, numeros, punto y "
            "guion (1 a 12 caracteres)."
        )
    return v


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MKT = "MKT"
    LMT = "LMT"


class OrderRequest(BaseModel):
    symbol: str
    side: Side
    quantity: float = Field(gt=0)
    order_type: OrderType = OrderType.MKT
    limit_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    # Si se especifica, la orden queda atada a un fondo (ver funds.py): la
    # contabilidad (cash_usd, posiciones, PnL realizado) se actualiza en ese
    # fondo en vez de mezclarse con el resto. None = cuenta general (sin fondo).
    fund_id: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def upper_symbol(cls, v: str) -> str:
        return validate_symbol(v)


class RuleViolation(BaseModel):
    rule: str
    message: str


class OrderDecision(BaseModel):
    approved: bool
    requires_manual_approval: bool
    violations: list[RuleViolation] = []
    estimated_value_usd: Optional[float] = None


class PositionSizeSuggestion(BaseModel):
    quantity: float
    risk_usd: float
    limited_by: Optional[str] = None


class AccountSummary(BaseModel):
    net_liquidation: float
    cash: float
    buying_power: float
    daily_pnl: float
    daily_pnl_pct: float
    currency: str = "USD"
    # False si IBKR todavia no entrego ningun dato de PnL diario real (recien
    # conectado, antes del primer callback de reqPnL) o si NetLiquidation es 0
    # (cuenta sin datos). En ese caso daily_pnl/daily_pnl_pct quedan en 0.0 por
    # default, pero ese 0.0 NO significa "sin perdida hoy": significa "no hay
    # dato todavia". Tratar ambos casos igual permitia que el kill switch
    # (_risk_monitor_loop) y RulesEngine.evaluate() fallaran ABIERTOS -- nunca
    # bloqueaban nada -- exactamente cuando menos se puede confiar en el dato.
    pnl_data_available: bool = True


class Position(BaseModel):
    symbol: str
    quantity: float
    avg_cost: float
    market_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    # True = precio en tiempo real de IBKR; False = ultimo precio conocido
    # (del cierre anterior, mientras el mercado esta cerrado o sin datos live).
    price_is_live: bool = True


class PendingOrder(BaseModel):
    id: str
    order: OrderRequest
    decision: OrderDecision
    created_at: datetime
    status: str = "pending"  # pending | approved | rejected | executed
    source: str = "user"  # user | signal_engine
    # Estrategia que genero esta señal (ver _draft_fund_order_from_signal en
    # main.py), para mostrarla en el dashboard cuando hay mas de un fondo
    # siguiendo estrategias distintas. None para ordenes manuales (source=
    # "user") o para borradores creados antes de que existiera este campo.
    strategy_id: Optional[str] = None


class SignalResult(BaseModel):
    symbol: str
    as_of: datetime
    last_price: float
    score: float
    momentum_3m_pct: float
    momentum_1m_pct: float
    trend_ok: bool
    rsi: float
    avg_volume: float
    pct_from_52w_high: Optional[float] = None
    suggested_stop_loss_price: float
    suggested_stop_loss_pct: float
    passes_filters: bool
    notes: list[str] = []
    # Gates operativos individuales (liquidez, blackout de earnings, regimen de
    # mercado, cercania al maximo de 52 semanas). A diferencia de
    # passes_filters (que exige TODOS los filtros, incluidos los de calidad de
    # la estrategia, y controla los badges/draft manual en la UI), estos
    # cuatro son solo los que verifican que es operativamente seguro entrar
    # (no que el papel sea "bueno"): el trigger de auto-trading los exige
    # ademas de un score minimo, en vez de exigir passes_filters completo, asi
    # un score alto no se descarta por un filtro de calidad mas estricto que
    # el listón de auto-trading. regime_ok/near_high_ok son None cuando la
    # estrategia no aplica ese filtro (ej. Largo Plazo/Dividendos): None no
    # bloquea, solo bloquea un False explicito.
    liquidity_ok: bool = True
    earnings_ok: bool = True
    regime_ok: Optional[bool] = None
    near_high_ok: Optional[bool] = None
    # Estrategia que produjo esta fila (ver app/strategies/): permite mostrar
    # resultados de varias estrategias en la misma tabla sin ambiguedad.
    strategy_id: str = "momentum"
    # Sector GICS de app/sectors.py, o None si el ticker no esta clasificado
    # todavia. Se usa para mostrar el sector en el radar y para el limite de
    # concentracion por sector de RulesEngine.
    sector: Optional[str] = None
    # Datos fundamentales (estrategias Largo plazo / Dividendos). None para
    # Momentum/Oportunista, que no los consultan.
    pe_ratio: Optional[float] = None
    dividend_yield_pct: Optional[float] = None
    payout_ratio_pct: Optional[float] = None
    # Precio/valor libro (yfinance price_to_book), usado junto a pe_ratio en
    # el componente "value" de Largo Plazo. None si yfinance no lo reporta.
    price_to_book: Optional[float] = None
    # Fundamentales adicionales "casi gratis" (vienen en la misma respuesta de
    # info de yfinance que ya se pedia, sin requests extra): solo Largo plazo
    # y Dividendos las consultan, igual que el resto de los fundamentales de
    # arriba. peg_ratio entra al score de Largo plazo (extiende su tesis de
    # valoracion); el resto es informativo/notas, no afecta ningun score (ver
    # comentarios de gating en strategies/long_term.py y strategies/dividend.py).
    peg_ratio: Optional[float] = None
    beta: Optional[float] = None
    analyst_recommendation: Optional[str] = None
    insider_ownership_pct: Optional[float] = None
    institutional_ownership_pct: Optional[float] = None
    short_pct_of_float: Optional[float] = None
    current_ratio: Optional[float] = None
    quick_ratio: Optional[float] = None
    free_cash_flow: Optional[float] = None
    # Contexto tecnico adicional (ver strategies/common.py), informativo para
    # las 4 estrategias; solo entra en `score` (via score_components) para
    # Momentum/Oportunista, que son las dos basadas en señales de precio.
    macd_histogram_pct: Optional[float] = None
    bollinger_pct_b: Optional[float] = None
    sector_relative_strength_pct: Optional[float] = None
    # Sentimiento de noticias recientes (ver app/news_sentiment.py): solo se
    # calcula para el shortlist de mejores candidatos tras el ranking
    # cross-sectional (no todo el universo escaneado, por costo de la API de
    # Claude) y solo si news_sentiment_enabled esta prendido en la config
    # (apagado por defecto). None para el resto de las filas: "sin dato", no
    # "neutral" (ver apply_news_sentiment_adjustment).
    news_sentiment: Optional[str] = None  # positive | negative | neutral
    news_summary: Optional[str] = None
    # Componentes crudos (sin ponderar) que entraron en `score`, indexados por
    # nombre (ver app/scoring.py). Vacio si la estrategia no los expone. Le
    # permite a scan() recalcular `score` con normalizacion cross-sectional
    # sin que evaluate_symbol() necesite saber nada de los demas simbolos del
    # batch.
    score_components: dict[str, float] = {}

    @property
    def operational_gates_ok(self) -> bool:
        """AND de los gates operativos (no de calidad). regime_ok/near_high_ok
        en None significa "no aplica para esta estrategia", no bloquea."""
        return (
            self.liquidity_ok
            and self.earnings_ok
            and (self.regime_ok is not False)
            and (self.near_high_ok is not False)
        )


class BacktestTrade(BaseModel):
    symbol: str
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str  # stop_loss | max_holding_days | trend_break
    # Alpha de ESTA operacion vs el benchmark (cfg.benchmark_symbol): return_pct
    # menos lo que hizo el benchmark close-a-close en la misma ventana exacta
    # entry_date->exit_date (no el periodo completo del backtest, que es lo
    # que ya compara benchmark_cumulative_return_pct a nivel resumen). None si
    # el benchmark no tiene ninguna cotizacion conocida en o antes de alguna
    # de las dos fechas (ver _trade_alpha_pct en backtest.py) -- nunca 0.0 por
    # default, porque 0.0 significa "empato exactamente con el benchmark", un
    # dato distinto de "no se pudo calcular".
    alpha_pct: Optional[float] = None
    # ATR al momento de la entrada, como % del precio de entrada. Usado por
    # _trade_weights en backtest.py para ponderar cada posicion por volatilidad
    # inversa (igual criterio que el sizing por riesgo en vivo, ver
    # rules.suggested_quantity) en vez de equiponderar. None si no se pudo
    # calcular (compatibilidad con datos historicos sin este campo).
    entry_atr_pct: Optional[float] = None
    # Distancia del stop-loss INICIAL como % del precio de entrada (stop_loss_
    # atr_multiplier * entry_atr_pct, calculado una sola vez al momento de la
    # entrada, sin reflejar ajustes posteriores del trailing stop). Insumo de
    # _risk_based_trade_weight en backtest.py para replicar el sizing por
    # riesgo de RulesEngine.suggested_quantity() (rules.py) dentro del
    # backtest: ese sizing usa el stop SUBMITIDO al colocar la orden, no uno
    # que despues trailea. None para operaciones sinteticas de test o datos
    # historicos guardados antes de este campo, igual criterio que
    # entry_atr_pct.
    stop_loss_pct: Optional[float] = None


class EquityCurvePoint(BaseModel):
    date: datetime
    equity_pct: float  # retorno acumulado de la estrategia (%), 0 al inicio


class BacktestSummary(BaseModel):
    start_date: datetime
    end_date: datetime
    total_trades: int
    win_rate_pct: float
    avg_return_pct: float
    avg_win_pct: float
    avg_loss_pct: float
    profit_factor: Optional[float] = None
    profit_factor_is_infinite: bool = False
    expectancy_pct: float
    strategy_cumulative_return_pct: float
    benchmark_cumulative_return_pct: float
    # Lo que hubiera devuelto el benchmark si CADA DIA se hubiera invertido
    # solo la misma fraccion de capital que la estrategia realmente tuvo
    # desplegada ese dia (la misma fraccion day-by-day que alimenta
    # avg_exposure_pct, ver _daily_equity_curve), compuesta sobre el camino
    # real de precios del benchmark -- no benchmark_cumulative_return_pct
    # (100% invertido los dos extremos del periodo) escalado de forma
    # estatica por un promedio. Comparar contra esta cifra (en vez de contra
    # benchmark_cumulative_return_pct) evita penalizar a la estrategia por el
    # cash ocioso que avg_exposure_pct ya muestra que tuvo: con exposicion
    # promedio baja, un retorno parcialmente invertido se ve injustamente
    # peor frente a un benchmark 100% invertido todo el tiempo.
    exposure_adjusted_benchmark_return_pct: float
    # Promedio de BacktestTrade.alpha_pct sobre TODAS las operaciones (no solo
    # las ultimas 50 de `trades`, igual que exit_reason_counts), excluyendo
    # las que no tienen alpha definido. None si ninguna operacion tiene alpha
    # definido (ej. el benchmark no cubre la ventana de ninguna operacion).
    avg_alpha_pct: Optional[float] = None
    max_drawdown_pct: float
    sharpe_ratio: Optional[float] = None
    # Probabilidad (0-100) de que el Sharpe ratio verdadero de la estrategia
    # sea mayor a cero, ajustada por sesgo de seleccion
    # (cfg.deflated_sharpe_num_trials variantes probadas) y por la
    # no-normalidad de los retornos diarios (skewness/kurtosis), siguiendo
    # Bailey & Lopez de Prado "The Deflated Sharpe Ratio" (2014). A diferencia
    # de sharpe_ratio (un ratio, puede ser cualquier numero real, y no avisa
    # si viene de pocas observaciones o de probar muchas variantes hasta
    # encontrar una buena), esto es una PROBABILIDAD: un valor bajo (ej. <95)
    # advierte que el sharpe_ratio observado podria ser puro azar/overfitting
    # de backtest, incluso si el numero crudo se ve bien. None con menos datos
    # de los que el calculo necesita para ser estable (ver
    # _deflated_sharpe_ratio_pct en backtest.py), igual que sharpe_ratio.
    deflated_sharpe_ratio_pct: Optional[float] = None
    # % promedio del capital asumido (backtest_assumed_capital_usd) que estuvo
    # efectivamente invertido en algun momento del periodo (1/top_n por cada
    # posicion abierta ese dia), no solo en los dias con una operacion activa
    # de muestra: un avg_exposure_pct bajo indica que la estrategia paso buena
    # parte del tiempo sin suficientes señales como para usar el capital
    # asignado a top_n posiciones.
    avg_exposure_pct: float = 0.0
    # Conteo de exit_reason sobre TODAS las operaciones (no solo las de
    # `trades`, que se trunca a las ultimas 50 para no inflar el payload del
    # dashboard en vivo).
    exit_reason_counts: dict[str, int] = {}
    trades: list[BacktestTrade] = []
    equity_curve: list[EquityCurvePoint] = []


class WalkForwardFold(BaseModel):
    start_date: datetime
    end_date: datetime
    total_trades: int
    # None cuando el fold no tuvo ninguna operacion: con 0 trades estas
    # metricas no estan definidas (ver _build_walk_forward_result).
    win_rate_pct: Optional[float] = None
    avg_return_pct: Optional[float] = None
    strategy_cumulative_return_pct: Optional[float] = None
    benchmark_cumulative_return_pct: Optional[float] = None
    # Mismas dos metricas nuevas que BacktestSummary (ver ahi para el detalle),
    # calculadas solo dentro de la ventana de este fold. None junto con el
    # resto de las metricas cuando el fold no tuvo ninguna operacion.
    exposure_adjusted_benchmark_return_pct: Optional[float] = None
    avg_alpha_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None


class WalkForwardResult(BaseModel):
    n_folds: int
    folds: list[WalkForwardFold]
