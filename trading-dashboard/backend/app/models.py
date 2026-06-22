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


class PendingOrder(BaseModel):
    id: str
    order: OrderRequest
    decision: OrderDecision
    created_at: datetime
    status: str = "pending"  # pending | approved | rejected | executed
    source: str = "user"  # user | signal_engine


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
    # Componentes crudos (sin ponderar) que entraron en `score`, indexados por
    # nombre (ver app/scoring.py). Vacio si la estrategia no los expone. Le
    # permite a scan() recalcular `score` con normalizacion cross-sectional
    # sin que evaluate_symbol() necesite saber nada de los demas simbolos del
    # batch.
    score_components: dict[str, float] = {}


class BacktestTrade(BaseModel):
    symbol: str
    entry_date: datetime
    exit_date: datetime
    entry_price: float
    exit_price: float
    return_pct: float
    exit_reason: str  # stop_loss | max_holding_days | score_exit


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
    max_drawdown_pct: float
    sharpe_ratio: Optional[float] = None
    # % promedio del capital asumido (backtest_assumed_capital_usd) que estuvo
    # efectivamente invertido en algun momento del periodo (1/top_n por cada
    # posicion abierta ese dia), no solo en los dias con una operacion activa
    # de muestra: un avg_exposure_pct bajo indica que la estrategia paso buena
    # parte del tiempo sin suficientes señales como para usar el capital
    # asignado a top_n posiciones.
    avg_exposure_pct: float = 0.0
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
    max_drawdown_pct: Optional[float] = None
    sharpe_ratio: Optional[float] = None


class WalkForwardResult(BaseModel):
    n_folds: int
    folds: list[WalkForwardFold]
