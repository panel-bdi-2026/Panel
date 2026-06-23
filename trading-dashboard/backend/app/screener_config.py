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
    stop_loss_atr_multiplier: float = 2.0
    max_holding_days: int = 15

    # Pesos normalizados a suma 1.0 (percentiles 0-100 por componente => el
    # score promedio de un simbolo "del monton" es ~50, no un multiplo de
    # 50): permite comparar el score directamente contra el mismo umbral que
    # usa el trigger de auto-trading en vivo (ver _live_score_entry_threshold
    # en main.py), sin tener que escalarlo primero.
    score_weight_momentum: float = 0.3077
    score_weight_volatility: float = 0.1538
    score_weight_rsi_recovery: float = 0.1538
    score_weight_room_to_grow: float = 0.1538
    # MACD recien cruzando a alcista es, literalmente, la señal de "giro al
    # alza" que esta estrategia busca: encaja con su tesis mejor que las
    # Bandas de Bollinger (que premiarian estar cerca de la banda superior,
    # lo opuesto al "espacio de crecimiento" que room_to_grow ya valora).
    score_weight_macd_turn: float = 0.1154
    score_weight_sector_relative_strength: float = 0.1154

    # Backtest score-driven (ver backtest.py): reemplaza el AND booleano de
    # filtros tecnicos por el mismo score percentil cross-sectional que usa
    # el scan en vivo (contra el resto del universo, ese mismo dia). Entra
    # cuando el score supera backtest_score_entry_threshold y sale cuando cae
    # por debajo de backtest_score_exit_threshold (o por stop-loss/tiempo
    # maximo, lo que ocurra primero). Mismo umbral se usa como gatillo de
    # auto-trading en el scan en vivo (ver _live_score_entry_threshold en
    # main.py): una sola fuente de verdad para "que tan bueno es lo bastante
    # bueno" en esta estrategia, en vez de duplicar el numero.
    backtest_score_entry_threshold: float = 57.7
    backtest_score_exit_threshold: float = 38.5


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

    # Pausa entre cada simbolo del universo durante un scan (segundos). Con un
    # universo grande (ej. S&P 500 completo) escanear sin pausa manda cientos
    # de pedidos seguidos a la API gratuita de Yahoo Finance, lo que puede
    # gatillar un bloqueo temporal.
    scan_request_delay_seconds: float = 0.15

    # Filtro de tendencia: precio > SMA rapida > SMA lenta.
    sma_fast: int = 20
    sma_slow: int = 50

    # Momentum: retorno (%) en cada ventana.
    momentum_lookback_days: int = 63  # ~3 meses
    momentum_short_days: int = 21  # ~1 mes

    rsi_period: int = 14
    rsi_min: float = 40
    rsi_max: float = 75

    # Pesos del score de ranking de Momentum (ver evaluate_symbol en
    # screener.py), normalizados a suma 1.0 (percentiles 0-100 por componente
    # => el score promedio de un simbolo "del monton" es ~50, comparable
    # directamente contra backtest_score_entry_threshold, que tambien sirve
    # de umbral de auto-trading en vivo, ver mas abajo).
    score_weight_relative_strength: float = 0.25
    score_weight_momentum_3m: float = 0.1786
    score_weight_momentum_1m: float = 0.1071
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

    # Backtest score-driven (ver backtest.py): mismo mecanismo que el de
    # OpportunisticConfig.backtest_score_entry_threshold/_exit_threshold.
    # regime_filter y near_high_filter NO son parte del score (son gates
    # booleanos puros, ver screener.py) y siguen aplicandose ademas del
    # umbral de score, igual que en el scan en vivo. Mismo umbral se usa
    # como gatillo de auto-trading en el scan en vivo (ver
    # _live_score_entry_threshold en main.py).
    backtest_score_entry_threshold: float = 57.1
    backtest_score_exit_threshold: float = 39.3

    # Volumen promedio en DOLARES (precio x acciones), no en cantidad de
    # acciones: una accion barata puede superar un umbral de acciones y
    # seguir siendo poco liquida en terminos de dinero realmente operado.
    min_avg_dollar_volume: float = 1_000_000
    top_n: int = 10

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
    # mayor score MAXIMO entre las 4 estrategias (ver _scan_general en
    # main.py), recalculados cada vez que se refresca el cache de señales.
    # Tratar como techo, no como objetivo fijo: dejar margen bajo 100 (el
    # piso gratuito de IBKR) para las lineas de rotacion del resto del
    # universo y las posiciones abiertas (broker.get_positions tambien
    # consume lineas).
    live_hot_symbols_cap: int = 50
    # Cuantos simbolos del resto del universo (los que no estan calientes) se
    # piden de a uno por ciclo de rotacion (cada poll_interval_seconds). Mas
    # alto rota el universo completo mas rapido, pero ocupa mas lineas libres
    # a la vez (no deberia superar 100 - live_hot_symbols_cap en la practica).
    live_rotation_batch_size: int = 25

    # Filtro de regimen de mercado: no se sugieren entradas largas si el
    # benchmark esta por debajo de su propia SMA de regimen (mercado en
    # tendencia bajista de fondo). Una estrategia long-only de momentum tiende
    # a funcionar mal en ese contexto aunque el papel individual pase los
    # demas filtros.
    regime_filter_enabled: bool = True
    regime_sma_period: int = 200

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

    # Escaneo proactivo en background: si esta habilitado, el backend corre el
    # screener solo (sin que el usuario abra el dashboard) cada
    # auto_scan_interval_minutes y arma ordenes de compra en borrador para
    # simbolos que recien empiezan a pasar los filtros (transicion no-pasa ->
    # pasa). Esas ordenes quedan en la cola de aprobacion manual igual que
    # cualquier otra: el motor nunca ejecuta nada por si solo.
    auto_scan_enabled: bool = False
    auto_scan_interval_minutes: int = 30
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
