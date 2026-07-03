from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from .atomic_io import atomic_write_text
from .models import validate_symbol

# Universo de partida: S&P 500 completo, para que el screener identifique
# oportunidades de forma autonoma en todo el indice en vez de depender de una
# lista corta armada a mano. Tickers con clase de accion (ej. BRK.B) usan guion
# en vez de punto para que coincidan con la convencion de yfinance. Editalo en
# screener.yaml segun tu criterio (un universo mas chico escanea mas rapido y
# consume menos cuota de la API gratuita de datos).
_SP500_TICKERS = [
    "A", "AAPL", "ABBV", "ABNB", "ABT", "ACGL", "ACN", "ADBE", "ADI", "ADM",
    "ADP", "ADSK", "AEE", "AEP", "AES", "AFL", "AIG", "AIZ", "AJG", "AKAM",
    "ALB", "ALGN", "ALL", "ALLE", "AMAT", "AMCR", "AMD", "AME", "AMGN", "AMP",
    "AMT", "AMZN", "ANET", "AON", "AOS", "APA", "APD", "APH", "APO", "APP",
    "APTV", "ARE", "ARES", "ATO", "AVB", "AVGO", "AVY", "AWK", "AXON", "AXP",
    "AZO", "BA", "BAC", "BALL", "BAX", "BBY", "BDX", "BEN", "BF-B", "BG",
    "BIIB", "BKNG", "BKR", "BLDR", "BLK", "BMY", "BNY", "BR", "BRK-B", "BRO",
    "BSX", "BX", "BXP", "C", "CAG", "CAH", "CARR", "CASY", "CAT", "CB",
    "CBOE", "CBRE", "CCI", "CCL", "CDNS", "CDW", "CEG", "CF", "CFG", "CHD",
    "CHRW", "CHTR", "CI", "CIEN", "CINF", "CL", "CLX", "CMCSA", "CME", "CMG",
    "CMI", "CMS", "CNC", "CNP", "COF", "COHR", "COIN", "COO", "COP", "COR",
    "COST", "CPAY", "CPB", "CPRT", "CPT", "CRH", "CRL", "CRM", "CRWD", "CSCO",
    "CSGP", "CSX", "CTAS", "CTSH", "CTVA", "CVNA", "CVS", "CVX", "D", "DAL",
    "DASH", "DD", "DDOG", "DE", "DECK", "DELL", "DG", "DGX", "DHI", "DHR",
    "DIS", "DLR", "DLTR", "DOC", "DOV", "DOW", "DPZ", "DRI", "DTE", "DUK",
    "DVA", "DVN", "DXCM", "EA", "EBAY", "ECL", "ED", "EFX", "EG", "EIX",
    "EL", "ELV", "EME", "EMR", "EOG", "EQIX", "EQR", "EQT", "ERIE", "ES",
    "ESS", "ETN", "ETR", "EVRG", "EW", "EXC", "EXE", "EXPD", "EXPE", "EXR",
    "F", "FANG", "FAST", "FCX", "FDS", "FDX", "FDXF", "FE", "FFIV", "FICO",
    "FIS", "FISV", "FITB", "FIX", "FOX", "FOXA", "FRT", "FSLR", "FTNT", "FTV",
    "GD", "GDDY", "GE", "GEHC", "GEN", "GEV", "GILD", "GIS", "GL", "GLW",
    "GM", "GNRC", "GOOG", "GOOGL", "GPC", "GPN", "GRMN", "GS", "GWW", "HAL",
    "HAS", "HBAN", "HCA", "HD", "HIG", "HII", "HLT", "HON", "HOOD", "HPE",
    "HPQ", "HRL", "HSIC", "HST", "HSY", "HUBB", "HUM", "HWM", "IBKR", "IBM",
    "ICE", "IDXX", "IEX", "IFF", "INCY", "INTC", "INTU", "INVH", "IP", "IQV",
    "IR", "IRM", "ISRG", "IT", "ITW", "IVZ", "J", "JBHT", "JBL", "JCI",
    "JKHY", "JNJ", "JPM", "KDP", "KEY", "KEYS", "KHC", "KIM", "KKR", "KLAC",
    "KMB", "KMI", "KO", "KR", "KVUE", "L", "LDOS", "LEN", "LH", "LHX",
    "LII", "LIN", "LITE", "LLY", "LMT", "LNT", "LOW", "LRCX", "LULU", "LUV",
    "LVS", "LYB", "LYV", "MA", "MAA", "MAR", "MAS", "MCD", "MCHP", "MCK",
    "MCO", "MDLZ", "MDT", "MET", "META", "MGM", "MKC", "MLM", "MMM", "MNST",
    "MO", "MOS", "MPC", "MPWR", "MRK", "MRNA", "MRSH", "MS", "MSCI", "MSFT",
    "MSI", "MTB", "MTD", "MU", "NCLH", "NDAQ", "NDSN", "NEE", "NEM", "NFLX",
    "NI", "NKE", "NOC", "NOW", "NRG", "NSC", "NTAP", "NTRS", "NUE", "NVDA",
    "NVR", "NWS", "NWSA", "NXPI", "O", "ODFL", "OKE", "OMC", "ON", "ORCL",
    "ORLY", "OTIS", "OXY", "PANW", "PAYX", "PCAR", "PCG", "PEG", "PEP", "PFE",
    "PFG", "PG", "PGR", "PH", "PHM", "PKG", "PLD", "PLTR", "PM", "PNC",
    "PNR", "PNW", "PODD", "POOL", "PPG", "PPL", "PRU", "PSA", "PSKY", "PSX",
    "PTC", "PWR", "PYPL", "Q", "QCOM", "RCL", "REG", "REGN", "RF", "RJF",
    "RL", "RMD", "ROK", "ROL", "ROP", "ROST", "RSG", "RTX", "RVTY", "SATS",
    "SBAC", "SBUX", "SCHW", "SHW", "SJM", "SLB", "SMCI", "SNA", "SNDK", "SNPS",
    "SO", "SOLV", "SPG", "SPGI", "SRE", "STE", "STLD", "STT", "STX", "STZ",
    "SW", "SWK", "SWKS", "SYF", "SYK", "SYY", "T", "TAP", "TDG", "TDY",
    "TECH", "TEL", "TER", "TFC", "TGT", "TJX", "TKO", "TMO", "TMUS", "TPL",
    "TPR", "TRGP", "TRMB", "TROW", "TRV", "TSCO", "TSLA", "TSN", "TT", "TTD",
    "TTWO", "TXN", "TXT", "TYL", "UAL", "UBER", "UDR", "UHS", "ULTA", "UNH",
    "UNP", "UPS", "URI", "USB", "V", "VEEV", "VICI", "VLO", "VLTO", "VMC",
    "VRSK", "VRSN", "VRT", "VRTX", "VST", "VTR", "VTRS", "VZ", "WAB", "WAT",
    "WBD", "WDAY", "WDC", "WEC", "WELL", "WFC", "WM", "WMB", "WMT", "WRB",
    "WSM", "WST", "WTW", "WY", "WYNN", "XEL", "XOM", "XYL", "XYZ", "YUM",
    "ZBH", "ZBRA", "ZTS",
]

# Complemento al S&P 500: empresas mas chicas, mas jovenes y de mayor
# potencial de crecimiento (small/mid-cap) que el indice de las 500 mas
# grandes deja afuera por definicion. No es la replica mecanica de un indice
# (S&P 400/600 o Russell 2000): no hubo forma de descargar esos constituyentes
# completos de forma verificable desde este entorno (Wikipedia, iShares/SPDR y
# similares devuelven error al intentar leerlos). En su lugar, cada ticker de
# esta lista fue confirmado individualmente via busqueda web contra al menos
# una fuente financiera real (Yahoo Finance, Nasdaq, stockanalysis.com, etc.)
# para minimizar el riesgo de incluir un simbolo inventado, deslistado o mal
# escrito. Cubre sectores de alto crecimiento (IA/software, biotech,
# fintech, ciberseguridad, semiconductores, espacio/defensa, robotica,
# vehiculos electricos/baterias, computacion cuantica, nuclear/SMR, minerales
# criticos) para diversificar la oportunidad mas alla de las mismas 500
# empresas grandes de siempre. Lista a revisar periodicamente: estas
# compañias son mas volatiles y menos liquidas que el S&P 500, por lo que el
# filtro de min_avg_dollar_volume es el que las saca del scan si se vuelven
# demasiado ilíquidas.
#
# Sesgo de look-ahead de INCLUSION: cada nombre se busco hoy a mano, lo que
# en la practica selecciona simbolos que YA se sabe que tuvieron una corrida
# fuerte reciente (IONQ, RGTI, OKLO, CRCL, SMCI, etc). Un backtest historico
# sobre este universo "encontraria" ganadores que estan en la lista PORQUE ya
# se sabe que ganaron -- algo que ningun scan corrido en tiempo real en el
# pasado podria haber replicado. Por eso backtest.py excluye estos tickers
# del universo que realmente simula (ver GROWTH_TICKERS/_backtest_universe
# ahi); el scan en vivo (screener.py y strategies/*.py) si los incluye sin
# ese problema, porque ahi se evaluan con datos de HOY para decidir una
# entrada HOY, no para medir un resultado historico ya conocido.
GROWTH_TICKERS = [
    "ABAT", "ACHR", "ACMR", "ADMA", "AEHR", "ALRM", "AMBA", "AMPX", "AMRC", "AOSL",
    "ASYS", "ATLX", "AVAV", "BB", "BKSY", "CAMT", "CEVA", "CHYM", "COHU", "CRBU",
    "CRCL", "CRML", "CRNX", "CRWV", "CURO", "ELVA", "EOSE", "FIVN", "FLNC", "GDOT",
    "GRC", "GWH", "ICHR", "INO", "INOD", "IONQ", "JOBY", "KLIC", "KRMD", "KTOS",
    "LC", "LUNR", "MAMA", "MDXH", "MP", "MUX", "MWA", "NAK", "NG", "OKLO",
    "OMCL", "ONDS", "OTLY", "OUST", "PATH", "PVLA", "QBTS", "QFIN", "QLYS", "QS",
    "QUBT", "RDW", "RDWR", "RGTI", "RKLB", "RPAY", "RR", "RUN", "S", "SCWX",
    "SERV", "SEZL", "SGMO", "SIDU", "SLDP", "SOFI", "SOUN", "STEM", "TDC", "TERN",
    "TMC", "USAR", "VKTX", "VRNS", "WTTR", "ZS",
]

DEFAULT_UNIVERSE = _SP500_TICKERS + GROWTH_TICKERS

STRATEGY_IDS = ("momentum", "opportunistic", "long_term", "dividend")


class OpportunisticConfig(BaseModel):
    """Estrategia de corto plazo (dias/semanas): busca acciones algo mas
    volatiles que ya muestran señales de giro al alza (RSI recuperandose
    desde zona baja) y que todavia cotizan bien por debajo de su maximo de 52
    semanas (espacio de crecimiento), a diferencia de Momentum que busca
    lideres ya en tendencia cerca de maximos."""

    momentum_lookback_days: int = 10  # ~2 semanas
    rsi_period: int = 14
    rsi_min: float = 35
    rsi_max: float = 60
    # ATR como % del precio: piso de volatilidad para calificar como "un poco
    # mas volatil" (si no, el filtro deja pasar nombres tan tranquilos como
    # los de Momentum, que no es el objetivo de esta estrategia).
    min_volatility_pct: float = 3.0
    # Techo solo para el componente de score (no gating, sin filtro de
    # volatilidad maxima): mas alla de este punto, mas volatilidad ya no
    # suma al score de "giro al alza" (band_score en common.py), es solo
    # mas riesgo sin contrapartida en la tesis de esta estrategia.
    max_volatility_pct: float = 12.0
    # Cuanto debe estar por debajo del maximo de 52 semanas (lo opuesto al
    # filtro de "cerca del maximo" de Momentum): da el espacio de crecimiento.
    min_pct_below_52w_high: float = 10.0
    # Techo solo para el componente de score (no gating): mas alla de este
    # punto ya no es "espacio de crecimiento" sino una caida sostenida que
    # probablemente señala un problema de fondo, no una oportunidad de giro.
    max_pct_below_52w_high: float = 30.0

    # Multiplicador ATR para el stop-loss. 1.5x sobre la volatilidad minima
    # de 3% da un stop de ~4.5%, dentro del techo global max_stop_loss_pct
    # (5%) sin necesitar ajuste forzado. El valor anterior (2.0x) producía
    # stops de ~6%+ que el tope global recortaba a 5%, dejando el stop a solo
    # 1.67x ATR — demasiado estrecho para el ruido normal de nombres con
    # alta volatilidad intradía.
    stop_loss_atr_multiplier: float = 1.5
    max_holding_days: int = 15

    # Parámetros del band_score de momentum (ROC de corto plazo).
    # La estrategia busca GIROS TEMPRANOS, no rallies ya avanzados: premiar
    # ROC muy alto selecciona acciones que ya rebotaron, no las que recién
    # están girando. band_score centra el ideal en ~3% (apenas positivo =
    # señal de giro incipiente) y decae a 0 en 11%+ (rally ya maduro).
    momentum_ideal_pct: float = 3.0
    momentum_half_range_pct: float = 8.0

    # Parámetros del band_score de RSI recovery.
    # Ideal: RSI ~45 (recuperación temprana, todavía sin sobrecompra).
    # Con half_range=12.5: RSI=35 (mínimo del gate) → 20 pts, RSI=45 → 100,
    # RSI≥57.5 → 0 pts. Antes era lineal (RSI=60 puntuaba casi el doble que
    # RSI=42), lo que favorecía RSIs ya altos — lo contrario de lo que busca
    # una estrategia de reversión temprana.
    rsi_recovery_ideal: float = 45.0
    rsi_recovery_half_range: float = 12.5

    # Días de volumen reciente para el componente volume_surge
    # (promedio_N_días / promedio_20d). Un ratio >1 indica participación
    # institucional en el rebote, señal de durabilidad del giro documentada
    # en la literatura de reversión de media.
    volume_surge_lookback_days: int = 3

    # Pesos normalizados a suma 1.0 (percentiles 0-100 por componente):
    #
    # momentum (era 30.77%): reducido a 15% — en una estrategia de reversal
    #   el ROC corto ya avanzado no predice magnitud del rebote restante;
    #   sigue siendo gate duro (roc>0), pero no domina el ranking.
    # rsi_recovery (era 15.38%): subido a 20% — posición en la zona de
    #   recuperación temprana es el corazón de la tesis.
    # macd_turn (era 11.54%): subido a 20% — el cruce alcista del MACD es
    #   la señal de giro más directa de la estrategia.
    # room_to_grow (sin cambio relativo): 15% — upside estructural.
    # volatility (era 15.38%): bajado a 10% — ya cubierto por el gate duro.
    # sector_relative_strength (era 11.54%): bajado a 10%.
    # volume_surge (nuevo): 10% — confirmación de participación institucional.
    score_weight_momentum: float = 0.15
    score_weight_volatility: float = 0.10
    score_weight_rsi_recovery: float = 0.20
    score_weight_room_to_grow: float = 0.15
    score_weight_macd_turn: float = 0.20
    score_weight_sector_relative_strength: float = 0.10
    score_weight_volume_surge: float = 0.10

    # Backtest score-driven (ver backtest.py): reemplaza el AND booleano de
    # filtros tecnicos por el mismo score percentil cross-sectional que usa
    # el scan en vivo (contra el resto del universo, ese mismo dia) para
    # decidir la ENTRADA. La salida no usa score (sale por stop-loss, tiempo
    # maximo o ruptura de tendencia, igual que _check_fund_exit en main.py:
    # no existe una salida por score en la cuenta en vivo). Mismo umbral se
    # usa como gatillo de auto-trading en el scan en vivo (ver
    # _live_score_entry_threshold en main.py): una sola fuente de verdad para
    # "que tan bueno es lo bastante bueno" en esta estrategia, en vez de
    # duplicar el numero.
    backtest_score_entry_threshold: float = 57.7


class LongTermConfig(BaseModel):
    """Estrategia de largo plazo (meses/1 año): fundamentales fuertes
    (crecimiento, rentabilidad) y precio bajo respecto a su valoracion
    (PE bajo), buscando apreciacion en el mediano plazo. No es backtesteable
    con datos gratuitos de yfinance (sin historia de fundamentals point-in-
    time): solo disponible para escaneo en vivo."""

    max_pe_ratio: float = 25.0
    min_revenue_growth_pct: float = 5.0
    min_earnings_growth_pct: float = 5.0
    min_return_on_equity_pct: float = 10.0
    max_debt_to_equity: float = 150.0
    min_profit_margin_pct: float = 5.0
    # PEG (PE / crecimiento de ganancias) solo a modo de nota/bonus de score,
    # no gating: a diferencia del PE (value_ok bloquea el filtro), un PEG
    # ausente o alto no descarta el simbolo por si solo, ya que el filtro de
    # valoracion principal (PE) y de crecimiento (earnings_growth) ya cubren
    # ambos lados de la misma tesis por separado.
    max_peg_ratio: float = 2.0
    # Riesgo de volatilidad (beta) e interes en corto: solo generan una nota
    # informativa (no bloquean ni puntuan), igual que max_debt_to_equity.
    max_beta: float = 1.5
    max_short_interest_pct: float = 20.0
    stop_loss_atr_multiplier: float = 2.5

    score_weight_value: float = 0.25
    score_weight_growth: float = 0.20
    # "quality" es un Piotroski-lite: combina rentabilidad (ROE, margen, que
    # antes era un componente "margin" separado) con una penalizacion graduada
    # por apalancamiento (debt_to_equity), todo con los mismos datos snapshot
    # que ya se pedian (sin requests ni costo adicional). Ver
    # strategies/long_term.py.
    score_weight_quality: float = 0.20
    # PEG extiende la tesis de valoracion (PE ajustado por crecimiento): mas
    # bajo es mejor, igual logica que value_score pero con max_peg_ratio como
    # tope en vez de max_pe_ratio.
    score_weight_peg: float = 0.10
    # safety (liquidez corriente/rapida, flujo de caja libre, beta) y
    # ownership_alignment (insiders e institucionales) son nuevos componentes
    # de score que antes solo aparecian como notas informativas (no gating):
    # ver safety_score/ownership_alignment_score en strategies/common.py.
    score_weight_safety: float = 0.15
    score_weight_ownership_alignment: float = 0.10

    # Umbral de score para el trigger de auto-trading en el scan en vivo (ver
    # _live_score_entry_threshold en main.py). Distinto de Momentum/
    # Oportunista, que reusan su backtest_score_entry_threshold: Largo Plazo
    # no es backtesteable (sin historia point-in-time de fundamentals), asi
    # que no existe un campo de backtest para reusar.
    live_score_entry_threshold: float = 60.0


class DividendConfig(BaseModel):
    """Estrategia de dividend yield: prioriza buen yield con un payout ratio
    sostenible (ni muy bajo ni tan alto que arriesgue un recorte), valorando
    tambien fundamentales sanos (ROE, margen). Tampoco es backtesteable con
    datos gratuitos de yfinance: solo escaneo en vivo."""

    min_dividend_yield_pct: float = 3.0
    # Techo solo para el componente de score (no gating, sin filtro de yield
    # maximo): un yield muy por encima de este punto suele ser señal de
    # "yield trap" (precio castigado por riesgo de recorte), no de mejor
    # oportunidad -- band_score en common.py puntua mejor cerca del centro
    # de este rango que en cualquiera de los dos extremos.
    max_dividend_yield_pct: float = 9.0
    min_payout_ratio_pct: float = 20.0
    max_payout_ratio_pct: float = 75.0
    min_return_on_equity_pct: float = 8.0
    min_profit_margin_pct: float = 5.0
    # Riesgo de volatilidad (beta) e interes en corto: solo generan una nota
    # informativa (no bloquean ni puntuan). Un inversor de dividendos suele
    # preferir baja volatilidad, pero eso es una preferencia de riesgo, no
    # parte de la tesis de yield/payout/calidad que ya puntua el score.
    max_beta: float = 1.5
    max_short_interest_pct: float = 20.0
    stop_loss_atr_multiplier: float = 2.5

    score_weight_yield: float = 0.35
    score_weight_payout_quality: float = 0.20
    score_weight_quality: float = 0.20
    # safety (liquidez corriente/rapida, flujo de caja libre, beta) y
    # ownership_alignment (insiders e institucionales) son nuevos componentes
    # de score, igual que en LongTermConfig (ver strategies/common.py):
    # antes solo aparecian como notas informativas, no gating.
    score_weight_safety: float = 0.15
    score_weight_ownership_alignment: float = 0.10

    # Umbral de score para el trigger de auto-trading en el scan en vivo (ver
    # _live_score_entry_threshold en main.py). Dividendos tampoco es
    # backtesteable, igual que Largo Plazo: no hay campo de backtest que
    # reusar.
    live_score_entry_threshold: float = 60.0


class ScreenerConfig(BaseModel):
    # Estrategia activa para el escaneo proactivo en background y el
    # auto-trading (ver _run_signal_scan_cycle en main.py): un solo valor
    # determinista, ya que ese ciclo corre sin intervencion del usuario. El
    # dashboard puede explorar cualquier estrategia ad-hoc via el query param
    # strategy_id de /api/signals/scan sin cambiar este valor persistido.
    strategy_id: str = "momentum"

    # Tope generoso por encima del S&P 500 completo (~500 simbolos): permite
    # personalizar el universo sin abrir la puerta a una lista descomunal que
    # haga un scan tardar horas o agote la cuota de la API de datos.
    universe: list[str] = Field(default=DEFAULT_UNIVERSE, max_length=1000)
    benchmark_symbol: str = "SPY"
    lookback_days: int = 400

    # Umbral de alerta de degradacion PARCIAL del feed de datos (% del
    # universo con fallo de get_daily_bars vigente -- ver
    # market_data.get_bars_failure_stats y _check_market_data_degradation en
    # main.py). Distinto del caso "0 de N simbolos respondieron", que ya
    # corta el scan de entrada con MarketDataError: este cubre el caso mas
    # insidioso de un feed que solo falla para una FRACCION del universo,
    # donde el scan "funciona" con menos simbolos evaluados sin ninguna señal
    # visible de que los scores/rankings actuales estan basados en datos
    # incompletos.
    market_data_degradation_alert_pct: float = Field(default=20, gt=0, le=100)

    # Pausa entre cada simbolo del universo durante un scan (segundos). Con un
    # universo grande (ej. S&P 500 completo) escanear sin pausa manda cientos
    # de pedidos seguidos a la API gratuita de Yahoo Finance, lo que puede
    # gatillar un bloqueo temporal.
    scan_request_delay_seconds: float = 0.15

    # Filtro de tendencia: precio > SMA rapida > SMA lenta.
    sma_fast: int = 20
    sma_slow: int = 50

    # Momentum: retorno (%) en cada ventana. momentum_lookback_days y
    # momentum_short_days alimentan momentum_3m_pct/momentum_1m_pct (campos de
    # contexto/display de SignalResult) y relative_strength (vs. el benchmark);
    # NO alimentan mas el score de Momentum, que usa el momentum 12-1 de abajo.
    momentum_lookback_days: int = 63  # ~3 meses
    momentum_short_days: int = 21  # ~1 mes

    # Momentum "12-1" (Jegadeesh & Titman, ver indicators.momentum_12_1):
    # retorno entre t-momentum_12_1_lookback_days y t-momentum_12_1_skip_days,
    # saltando el ultimo mes para no premiar la reversion de corto plazo que
    # domina ese tramo -- a diferencia del viejo blend momentum_3m + momentum_1m
    # (score_weight_momentum_3m/1m, eliminados), que sumaba ese tramo en vez de
    # excluirlo. Reemplaza esos dos componentes en el score (ver evaluate_symbol
    # en screener.py); son campos nuevos (no se reusan momentum_lookback_days/
    # momentum_short_days) para no romper la semantica de los campos de
    # display/contexto de arriba, que siguen siendo ventanas de 3m/1m de verdad.
    momentum_12_1_lookback_days: int = 252  # ~12 meses
    momentum_12_1_skip_days: int = 21  # ~1 mes salteado

    rsi_period: int = 14
    rsi_min: float = 40
    rsi_max: float = 75

    # Pesos del score de ranking de Momentum (ver evaluate_symbol en
    # screener.py), normalizados a suma 1.0 (percentiles 0-100 por componente
    # => el score promedio de un simbolo "del monton" es ~50, comparable
    # directamente contra backtest_score_entry_threshold, que tambien sirve
    # de umbral de auto-trading en vivo, ver mas abajo).
    score_weight_relative_strength: float = 0.25
    score_weight_momentum_12_1: float = 0.2857
    score_weight_trend: float = 0.1071
    score_weight_rsi: float = 0.0714
    # Señales tecnicas adicionales (confirmacion de tendencia, no gating):
    # MACD e indice %B de Bollinger refuerzan la misma tesis de momentum ya
    # confirmado (cerca/sobre la banda superior, histograma positivo), y la
    # fuerza relativa contra el ETF del propio sector (distinta de
    # score_weight_relative_strength, que es contra el benchmark general).
    score_weight_macd: float = 0.0714
    score_weight_bollinger: float = 0.0714
    score_weight_sector_relative_strength: float = 0.1429

    # Backtest score-driven (ver backtest.py): mismo mecanismo que
    # OpportunisticConfig.backtest_score_entry_threshold, solo para la
    # ENTRADA. regime_filter, near_high_filter y la liquidez minima NO son
    # parte del score (son gates booleanos puros, ver screener.py) y siguen
    # aplicandose ademas del umbral de score, igual que en el scan en vivo.
    # La salida no usa score (ver _simulate_symbol en backtest.py). Mismo
    # umbral se usa como gatillo de auto-trading en el scan en vivo (ver
    # _live_score_entry_threshold en main.py).
    backtest_score_entry_threshold: float = 57.1

    # Volumen promedio en DOLARES (precio x acciones), no en cantidad de
    # acciones: una accion barata puede superar un umbral de acciones y
    # seguir siendo poco liquida en terminos de dinero realmente operado.
    min_avg_dollar_volume: float = 1_000_000
    top_n: int = 10
    # Tope de posiciones concurrentes ABIERTAS A LA VEZ en el mismo sector (ver
    # get_sector en sectors.py), aplicado por cap_concurrent_positions en
    # backtest.py. top_n por si solo no evita concentracion: nada impide
    # terminar con, por ejemplo, 10 semiconductoras a la vez (una sola apuesta
    # sectorial disfrazada de 10 posiciones diversificadas). 0 = sin tope (solo
    # el de top_n), mismo criterio "0/invalido deshabilita" que top_n.
    max_concurrent_positions_per_sector: int = 0

    atr_period: int = 14
    stop_loss_atr_multiplier: float = 1.5
    max_holding_days: int = 20

    # Trailing stop para posiciones abiertas por el motor de auto-trading (ver
    # _check_fund_trailing_stop en main.py): si esta habilitado, el monitor de
    # salida sube (nunca baja) el stop-loss ya colocado en IBKR a medida que
    # el precio se mueve a favor, usando la misma distancia en ATR que el
    # stop inicial (stop_loss_atr_multiplier) sobre el ATR de cada chequeo.
    # Apagado por defecto: cambia el perfil de riesgo de "stop fijo" a "stop
    # que persigue el precio", y eso conviene que sea una decision explicita
    # del usuario, no el comportamiento nuevo por defecto de una version
    # anterior que nunca lo tuvo.
    trailing_stop_enabled: bool = False

    # Salida parcial (scale-out): si esta habilitado, cuando una posicion
    # abierta por el motor de auto-trading alcanza scale_out_at_r_multiple
    # veces su riesgo inicial en ganancia no realizada (medido en "R", donde
    # 1R = distancia entre el precio de entrada y el stop-loss INICIAL, no el
    # actual -- ver FundPosition.initial_stop_loss_price), el monitor de
    # salida vende scale_out_pct% de la posicion y mueve el stop-loss del
    # remanente a breakeven (avg_cost). Esto asegura parte de la ganancia sin
    # cerrar la posicion entera, y deja el resto corriendo sin riesgo de
    # perdida neta en esa posicion. Solo se aplica una vez por posicion (ver
    # FundPosition.scaled_out_at): no repite la venta parcial en cada ciclo
    # del monitor. Apagado por defecto, mismo motivo que trailing_stop_enabled
    # (cambia el perfil de riesgo, debe ser una decision explicita).
    scale_out_enabled: bool = False
    scale_out_at_r_multiple: float = Field(default=1.5, gt=0, le=10)
    scale_out_pct: float = Field(default=50, gt=0, lt=100)

    # Radar en vivo: mantiene un subconjunto "caliente" de simbolos con
    # streaming persistente de IBKR (Nivel 1, gratis hasta 100 lineas
    # simultaneas) para que el precio mostrado en el radar sea casi
    # instantaneo en vez de depender del cache de yfinance (TTL de 15 min,
    # ver market_data.py). El resto del universo se refresca por rotacion de
    # snapshots con las lineas que el hot-set deja libres (ver
    # _hot_set_loop/_price_rotation_loop en main.py). Nunca toca
    # score/RSI/etc, que siguen viniendo del scan cacheado: solo pisa el
    # precio mostrado y marca is_hot.
    live_radar_enabled: bool = True
    # Tope de simbolos en streaming permanente. Los calientes son los de
    # mayor score MAXIMO entre las estrategias activas (ver _scan_general en
    # main.py), recalculados cada vez que se refresca el cache de señales.
    # Tratar como techo, no como objetivo fijo: dejar margen bajo 100 (el
    # piso gratuito de IBKR) para las lineas de rotacion del resto del
    # universo y las posiciones abiertas (broker.get_positions tambien
    # consume lineas).
    live_hot_symbols_cap: int = 50
    # Estrategias que alimentan el hot-set del radar en vivo. None = todas las
    # estrategias con resultados en cache (comportamiento por defecto). Si se
    # especifica una lista, solo los simbolos que rankean alto en alguna de esas
    # estrategias ocupan los slots de streaming: util cuando solo hay fondos
    # activos en un subconjunto de estrategias y no se quiere gastar lineas de
    # IBKR en stocks que ningun fondo puede operar.
    live_radar_strategy_filter: list[str] | None = None
    # Cuantos simbolos del resto del universo (los que no estan calientes) se
    # piden de a uno por ciclo de rotacion (cada poll_interval_seconds). Mas
    # alto rota el universo completo mas rapido, pero ocupa mas lineas libres
    # a la vez (no deberia superar 100 - live_hot_symbols_cap en la practica).
    live_rotation_batch_size: int = 25
    # Cuantos simbolos del universo del screener se refrescan de yfinance por
    # ciclo de _run_data_refresh_cycle (mismo patron que live_rotation_batch_size
    # pero para datos historicos, no precios en vivo). Mas alto acelera el
    # precalentamiento del cache pero puede disparar rate-limit de la API
    # gratuita de Yahoo Finance.
    data_refresh_batch_size: int = 25

    # Filtro de regimen de mercado para Momentum: no se sugieren entradas
    # largas si el benchmark no esta en un regimen alcista de fondo. Antes era
    # un cruce binario precio > SMA200, lento pero igual propenso a whipsaw
    # cuando la SMA esta plana (cruces de ida y vuelta sin que cambie el
    # regimen real). Ahora exige dos condiciones mas lentas a la vez (ver
    # indicators.market_regime_ok): la SMA de regimen tiene que estar en
    # pendiente positiva (no solo el precio por encima de ella), y el
    # benchmark tiene que tener momentum absoluto positivo (dual
    # momentum/Antonacci) en una ventana larga. Solo aplica a Momentum --
    # ver opportunistic_regime_filter_enabled para Oportunista.
    regime_filter_enabled: bool = True
    regime_sma_period: int = 200
    regime_slope_lookback_days: int = 20
    regime_absolute_momentum_lookback_days: int = 252

    # Mismo filtro de regimen (misma SMA/pendiente/momentum absoluto de
    # arriba) pero para Oportunista, con flag propio porque responde al
    # revés que Momentum: Oportunista compra giros/reversiones, que aparecen
    # justamente cuando el mercado no esta en tendencia alcista limpia.
    # Se agrego en commit f6ae7d3 (2026-06-23) compartiendo el flag de
    # Momentum bajo la hipotesis de que el filtro tambien lo protegeria a el
    # de "comprar cuchillos cayendo"; el re-test de ese cambio (experimento
    # P2, 2026-06-24, ya con el universo de backtest corregido por sesgo de
    # look-ahead) mostro lo opuesto: para Oportunista el filtro empeora
    # retorno acumulado y Sharpe en vez de mejorarlos. Default False por eso.
    opportunistic_regime_filter_enabled: bool = False

    # Dias antes de la fecha estimada de earnings en los que NO se sugieren
    # nuevas entradas: un gap por sorpresa de resultados puede saltarse un
    # stop-loss basado en ATR sin ejecutarse al precio sugerido.
    earnings_blackout_days: int = 5

    # Filtro de proximidad al maximo de 52 semanas: solo se consideran entradas
    # en simbolos que cotizan a no mas de max_pct_below_52w_high por debajo de
    # su maximo de 52 semanas. Comprar lideres cerca de maximos (breakouts) en
    # vez de nombres ya extendidos a la baja es un efecto momentum documentado
    # (George & Hwang, 2004): la cercania al maximo de 52 semanas predice
    # continuacion mejor que el retorno pasado por si solo. Si no hay historia
    # suficiente para el maximo, el filtro no bloquea (no falla por falta de dato).
    near_high_filter_enabled: bool = True
    max_pct_below_52w_high: float = 15.0

    backtest_years: int = 3
    # Costos de transaccion asumidos en el backtest (antes no se modelaban, lo
    # que infla artificialmente los retornos reportados respecto a la
    # operatoria real). backtest_assumed_capital_usd es solo una referencia
    # para convertir commission_per_trade_usd en % por operacion: el backtest
    # no rastrea dolares reales, solo retornos %.
    backtest_assumed_capital_usd: float = 100_000
    commission_per_trade_usd: float = 1.0
    slippage_pct: float = 0.05

    # slippage_pct fijo es razonable para nombres liquidos, pero subestima el
    # costo real de entrar/salir en microcaps o nombres de bajo volumen: ahi
    # el spread es mas ancho y una orden a mercado (o un gap) mueve el precio
    # mucho mas que en un nombre liquido (ver _effective_slippage_pct en
    # backtest.py). Si el volumen promedio en dolares del dia del fill (entrada
    # o salida, evaluados por separado: la liquidez puede cambiar entre una
    # punta y la otra de la misma operacion) esta por debajo de este umbral,
    # se multiplica slippage_pct por low_liquidity_slippage_multiplier para
    # ESE fill especifico. A diferencia de otras features nuevas del backtest,
    # esto va prendido por defecto: no es una eleccion de metodologia (como
    # equiponderar vs por volatilidad), es corregir un costo ya modelado pero
    # con un numero fijo irrealmente optimista para el segmento de baja
    # liquidez. 0 en el umbral deshabilita (ningun simbolo recibe el
    # multiplicador), mismo criterio "0 deshabilita" que el resto de los topes
    # del backtest.
    low_liquidity_dollar_volume_threshold: float = 5_000_000
    low_liquidity_slippage_multiplier: float = 3.0

    # El backtest equipondera cada posicion concurrente (1/top_n, ver
    # _daily_equity_curve en backtest.py), pero el sizing en vivo
    # (rules.suggested_quantity) es por riesgo ATR: dos esquemas distintos, asi
    # que el backtest no valida realmente lo que se opera en vivo. Si esta
    # activo, cada posicion pesa ~ 1/ATR en vez de igual (mismo criterio de
    # riesgo que el sizing en vivo: menos peso a lo mas volatil), lo que en
    # general sube el Sharpe a retorno similar. Apagado por defecto: cambia los
    # numeros historicos del backtest, asi que es una decision explicita del
    # usuario, no el comportamiento nuevo por defecto de una version anterior
    # que nunca lo tuvo.
    backtest_vol_weighting_enabled: bool = False

    # Mismo problema que backtest_vol_weighting_enabled (el backtest no
    # replica el sizing real), pero resuelto de forma exacta en vez de
    # aproximada: si esta activo, cada posicion simulada se dimensiona con la
    # MISMA formula que RulesEngine.suggested_quantity() (rules.py) usa en
    # vivo -- risk_per_trade_pct de la equity simulada / distancia al stop
    # (BacktestTrade.stop_loss_pct), acotado por max_position_pct_of_equity y
    # por max_order_value_usd convertido a fraccion de la equity simulada ese
    # dia (ver _risk_based_trade_weight en backtest.py) -- en vez de
    # equiponderar o ponderar por inversa de ATR. Los 3 parametros de riesgo
    # se leen de la config de RulesEngine YA EXISTENTE (pasada como argumento
    # a run_backtest/run_opportunistic_backtest), no de una copia duplicada
    # aca: asi no hay drift entre "lo que el backtest asume que se arriesga"
    # y lo que el motor de auto-trading en vivo arriesgaria realmente. Tiene
    # PRECEDENCIA sobre backtest_vol_weighting_enabled si los dos estan
    # activos (es la aproximacion mas fiel a lo que en realidad se operaria).
    # Apagado por defecto, mismo motivo que backtest_vol_weighting_enabled:
    # cambia los numeros historicos de equity/Sharpe/drawdown de cualquier
    # backtest ya corrido, asi que es una decision explicita del usuario.
    backtest_risk_based_sizing_enabled: bool = False

    # "Core+satelite" en el backtest: por defecto, el cash no invertido cada
    # dia (1 - exposicion, ver _daily_equity_curve en backtest.py) se asume
    # quieto, sin generar nada. Si esta activo, ese cash ocioso se simula
    # invertido dia por dia en el benchmark (mismo mecanismo de ponderacion
    # por exposicion que ya usa exposure_adjusted_benchmark_return_pct, pero
    # aplicado al COMPLEMENTO de la exposicion en vez de a la exposicion
    # misma), en vez de quedar a 0% fijo. Apagado por defecto: cambia el
    # perfil de retorno/riesgo de la curva de equity simulada, asi que es una
    # decision explicita del usuario, no el comportamiento nuevo por defecto
    # de una version anterior que nunca lo tuvo. Solo afecta al backtest: el
    # auto-trading en vivo no compra el benchmark automaticamente con el cash
    # libre, eso requeriria logica de rebalanceo en main.py/broker que esta
    # fuera del alcance de esta simulacion.
    invest_idle_cash_in_benchmark: bool = False

    # Cuantas variantes de parametros/estrategias se probaron (en este backtest
    # o en corridas previas) antes de quedarse con la configuracion actual:
    # insumo del Deflated Sharpe Ratio (ver _compute_summary_stats en
    # backtest.py), que penaliza el sharpe_ratio observado por el sesgo de
    # seleccion de buscar entre muchas variantes y quedarse con la que mejor
    # backtest dio (overfitting de backtest) -- un sharpe_ratio alto es mucho
    # menos confiable si fue el mejor de 50 intentos que si fue el unico
    # intento. 1 = sin ajuste por multiples pruebas (el valor por defecto, ya
    # que esta cuenta no se rastrea automaticamente entre corridas: el usuario
    # tiene que estimarla a mano segun cuantas configuraciones distintas probo
    # antes de esta).
    deflated_sharpe_num_trials: int = 1

    # Recalculo proactivo de scores en background: si esta habilitado, el
    # backend recalcula scores sobre el cache de datos (sin tocar la red) cada
    # auto_scan_interval_minutes. El refresco de datos lo hace _data_refresh_loop
    # por separado, en lotes chicos, de forma continua. Cuando un simbolo
    # transiciona de no-pasar a pasar los filtros, se arma una orden en borrador
    # para aprobacion manual.
    auto_scan_enabled: bool = False
    auto_scan_interval_minutes: int = 2
    # Tope de ordenes en borrador que el escaneo proactivo puede crear en un
    # solo ciclo. Si el regimen de mercado se vuelve alcista de golpe, decenas
    # de simbolos pueden pasar a cumplir los filtros a la vez; sin este tope se
    # draftearian todas juntas, cada una sizeada como si fuera la unica
    # posicion. Ademas del tope, no se draftea mas alla de los "cupos" libres
    # respecto a top_n contando lo que ya esta pendiente.
    max_auto_drafts_per_cycle: int = 3

    # Sentimiento de noticias via Claude Haiku 4.5 (ver app/news_sentiment.py):
    # ajuste acotado de score en una segunda etapa, posterior al ranking
    # cross-sectional, solo para el shortlist de los mejores candidatos (no
    # todo el universo: cada simbolo nuevo le pega a la API de Claude, que
    # tiene costo a diferencia del resto de market_data.py). Apagado por
    # defecto: requiere ANTHROPIC_API_KEY configurada y es una decision
    # explicita del usuario gastar en esto, no el comportamiento nuevo por
    # defecto de una version anterior que nunca lo tuvo.
    news_sentiment_enabled: bool = False
    # Tamaño del shortlist como multiplo de top_n (ej. top_n=10, multiplier=3
    # => se clasifican los 30 mejores tras el ranking, no los ~500 del
    # universo completo).
    news_sentiment_shortlist_multiplier: float = 3.0
    # Maximo que el sentimiento puede sumar/restar al score (ya en escala
    # percentil 0-100 tras apply_cross_sectional_normalization): positivo
    # suma este valor, negativo lo resta, neutral no mueve el score.
    news_sentiment_max_adjustment: float = 10.0

    # Configuracion especifica de las estrategias adicionales (ver
    # app/strategies/). Los campos compartidos arriba (universe, lookback_days,
    # min_avg_dollar_volume, atr_period, top_n, earnings_blackout_days, etc.)
    # se reutilizan para las cuatro estrategias en vez de duplicarlos en cada
    # bloque anidado.
    opportunistic: OpportunisticConfig = OpportunisticConfig()
    long_term: LongTermConfig = LongTermConfig()
    dividend: DividendConfig = DividendConfig()

    @field_validator("strategy_id")
    @classmethod
    def validate_strategy_id(cls, v: str) -> str:
        if v not in STRATEGY_IDS:
            raise ValueError(f"strategy_id invalido: {v!r}. Debe ser uno de {STRATEGY_IDS}.")
        return v

    @field_validator("universe")
    @classmethod
    def validate_universe(cls, v: list[str]) -> list[str]:
        # _sync_whitelist_with_universe() (main.py) copia este universo
        # directo a rules_config.symbol_whitelist, que RulesEngine.evaluate()
        # usa para aprobar o rechazar ordenes: sin la misma sanitizacion que
        # OrderRequest.symbol, un universo cargado a mano con un simbolo
        # malformado o sin normalizar quedaria en la whitelist sin que
        # ninguna orden real (siempre normalizada) pueda matchearlo nunca.
        return [validate_symbol(s) for s in v]

    @field_validator("benchmark_symbol")
    @classmethod
    def validate_benchmark_symbol(cls, v: str) -> str:
        return validate_symbol(v)

    @classmethod
    def load(cls, path: Path) -> "ScreenerConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = yaml.safe_load(path.read_text()) or {}
        return cls(**data)

    def save(self, path: Path) -> None:
        atomic_write_text(path, yaml.safe_dump(self.model_dump(), sort_keys=False))
