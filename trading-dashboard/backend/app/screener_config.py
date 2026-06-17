from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

# Universo de partida: S&P 500 completo, para que el screener identifique
# oportunidades de forma autonoma en todo el indice en vez de depender de una
# lista corta armada a mano. Tickers con clase de accion (ej. BRK.B) usan guion
# en vez de punto para que coincidan con la convencion de yfinance. Editalo en
# screener.yaml segun tu criterio (un universo mas chico escanea mas rapido y
# consume menos cuota de la API gratuita de datos).
DEFAULT_UNIVERSE = [
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


class ScreenerConfig(BaseModel):
    universe: list[str] = DEFAULT_UNIVERSE
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

    # Volumen promedio en DOLARES (precio x acciones), no en cantidad de
    # acciones: una accion barata puede superar un umbral de acciones y
    # seguir siendo poco liquida en terminos de dinero realmente operado.
    min_avg_dollar_volume: float = 1_000_000
    top_n: int = 10

    atr_period: int = 14
    stop_loss_atr_multiplier: float = 1.5
    max_holding_days: int = 20

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

    @classmethod
    def load(cls, path: Path) -> "ScreenerConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = yaml.safe_load(path.read_text()) or {}
        return cls(**data)

    def save(self, path: Path) -> None:
        path.write_text(yaml.safe_dump(self.model_dump(), sort_keys=False))
