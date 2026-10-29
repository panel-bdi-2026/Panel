from __future__ import annotations

import calendar
import math
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator

from .atomic_io import atomic_write_text
from .models import (
    AccountSummary,
    OrderDecision,
    OrderRequest,
    PositionSizeSuggestion,
    RuleViolation,
    Side,
    validate_symbol,
)


class RulesConfig(BaseModel):
    # Mismo tope que ScreenerConfig.universe (de donde normalmente se
    # sincroniza via _sync_whitelist_with_universe en main.py): un PUT directo
    # a /api/rules con symbol_whitelist sin esta validacion podia persistir
    # simbolos malformados o una lista descomunal sin el mismo chequeo que
    # OrderRequest.symbol.
    symbol_whitelist: list[str] = Field(default=[], max_length=1000)
    # Cotas superiores en los campos que limitan riesgo: sin ellas, PUT
    # /api/rules podia recibir un valor absurdamente alto (ej.
    # daily_loss_limit_pct=999999) que en la practica neutraliza el kill
    # switch sin desactivarlo explicitamente, ya que ninguna perdida diaria
    # real llegaria nunca a ese umbral.
    max_order_value_usd: float = Field(default=5000, gt=0, le=10_000_000)
    max_position_pct_of_equity: float = Field(default=10, gt=0, le=100)
    # Limite de exposicion combinada a un mismo sector GICS (posiciones
    # existentes en ese sector + la orden en evaluacion), como % del equity.
    # Solo se aplica si se puede determinar el sector de la orden (ver
    # app/sectors.py); si no, la regla no bloquea (sin dato, no se rechaza).
    max_sector_concentration_pct: float = Field(default=30, gt=0, le=100)
    # Exposicion BRUTA maxima de toda la cartera (todas las posiciones + la
    # orden en evaluacion), como % del equity -- a diferencia de
    # max_position_pct_of_equity (por simbolo) y max_sector_concentration_pct
    # (por sector), este es el unico limite que mira la cartera COMPLETA sin
    # importar como se reparte entre simbolos/sectores. Default 100%: nunca
    # invertir mas del equity disponible (sin margen implicito), salvo que se
    # suba explicitamente. le=1000 (no 100) para no impedirle a una cuenta
    # con margen habilitado configurar un tope por encima del 100% a
    # proposito.
    max_total_exposure_pct: float = Field(default=100, gt=0, le=1000)
    # "Portfolio heat": si TODOS los stops abiertos se tocaran a la vez (no
    # solo el de la operacion en evaluacion), cuanto se perderia en total,
    # como % del equity. Riesgo agregado de la cartera completa -- distinto
    # de risk_per_trade_pct (riesgo de UNA sola operacion): varias posiciones
    # con bajo riesgo individual pero correlacionadas (ej. todas en el mismo
    # regimen de mercado, aunque esten en sectores distintos) pueden sumar un
    # riesgo combinado mucho mayor al que cualquiera de los limites
    # individuales deja ver. Default 6%: conservador, unos pocos multiplos de
    # risk_per_trade_pct por defecto (1%).
    max_portfolio_heat_pct: float = Field(default=6, gt=0, le=100)
    # Riesgo maximo a arriesgar por operacion, como % del equity, si se toca el
    # stop-loss. Se usa solo para sugerir un tamano de posicion (no rechaza
    # ordenes por si solo): el tamano final igual queda limitado tambien por
    # max_position_pct_of_equity y max_order_value_usd.
    risk_per_trade_pct: float = Field(default=1, gt=0, le=100)
    # Monto mínimo en USD por orden de compra. Órdenes más pequeñas se descartan
    # antes de enviarse: con comisiones de ~$1/orden, una posición de $200 carga
    # un 0.5% solo al entrar. Por debajo de este umbral el drag supera el beneficio
    # de la diversificación extra. Aplica tanto a auto-trades como a borradores.
    min_transaction_usd: float = Field(default=500, gt=0, le=10_000_000)
    daily_loss_limit_pct: float = Field(default=2, gt=0, le=100)
    # Circuit breaker de drawdown ACUMULADO (no diario): a diferencia de
    # daily_loss_limit_pct (que se resetea cada dia junto con daily_pnl_pct de
    # IBKR, asi que una racha de perdidas repartida en varios dias por debajo
    # del umbral diario nunca la dispara), este mide la caida desde el maximo
    # historico de equity de la cuenta (ver state["peak_equity_usd"] en
    # main.py) y no se resetea nunca -- solo sube cuando la cuenta hace un
    # nuevo maximo. Default mas holgado que daily_loss_limit_pct (15% vs 2%)
    # a proposito: una caida acumulada tolerable en varias semanas de trading
    # normal seria un evento catastrofico si pasara en un solo dia.
    max_drawdown_pct: float = Field(default=15, gt=0, le=100)
    max_trades_per_day: int = Field(default=10, gt=0, le=1000)
    # Cupo diario ADICIONAL por fondo: max_trades_per_day sigue aplicando
    # como circuit-breaker global (cuenta ordenes de TODOS los fondos +
    # manuales sin distinguir), pero con 2+ fondos activos ese cupo
    # compartido dejaba que uno solo lo agotara y bloqueara a los demas por
    # el resto del dia. None (default) = sin cupo adicional por fondo, solo
    # el global. Solo se chequea para ordenes atadas a un fondo (fund_id
    # presente); ordenes de la cuenta general no tienen fondo del que contar.
    max_trades_per_day_per_fund: int | None = Field(default=None, gt=0, le=1000)
    require_stop_loss_on_buy: bool = True
    max_stop_loss_pct: float = Field(default=5, gt=0, le=100)
    allow_short_selling: bool = False
    manual_approval_threshold_usd: float = Field(default=1000, ge=0, le=100_000_000)
    allow_extended_hours: bool = False
    trading_hours_start: str = "09:30"
    trading_hours_end: str = "16:00"
    trading_hours_timezone: str = "America/New_York"

    @field_validator("symbol_whitelist")
    @classmethod
    def validate_symbol_whitelist(cls, v: list[str]) -> list[str]:
        return [validate_symbol(s) for s in v]

    @classmethod
    def load(cls, path: Path) -> "RulesConfig":
        if not path.exists():
            cfg = cls()
            cfg.save(path)
            return cfg
        data = yaml.safe_load(path.read_text()) or {}
        return cls(**data)

    def save(self, path: Path) -> None:
        atomic_write_text(path, yaml.safe_dump(self.model_dump(), sort_keys=False))


def _parse_hhmm(value: str) -> dtime:
    h, m = value.split(":")
    return dtime(int(h), int(m))


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """weekday: lunes=0 ... domingo=6. Fecha de la n-esima ocurrencia de ese
    dia de la semana en el mes (n=1 es la primera)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    offset = (last.weekday() - weekday) % 7
    return last - timedelta(days=offset)


def _easter_sunday(year: int) -> date:
    """Algoritmo gregoriano anonimo. Hace falta para Good Friday (el NYSE
    cierra ese dia), que no sigue una regla de "n-esimo lunes del mes"."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """Regla federal de EEUU: un feriado de fecha fija que cae sabado se
    observa el viernes anterior, y si cae domingo se observa el lunes
    siguiente."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nyse_holidays(year: int) -> set[date]:
    """Feriados de mercado CERRADO TODO EL DIA del NYSE, calculados por regla
    en vez de copiados de una tabla fija que se desactualiza cada anio. No
    incluye los dias de cierre anticipado (ej. el viernes despues de
    Thanksgiving): esos siguen operando dentro del horario configurado, solo
    con menos liquidez, y requieren una tabla propia que cambia mas seguido
    que estos feriados de dia completo."""
    holidays = {
        _observed(date(year, 1, 1)),  # Ano Nuevo
        _nth_weekday_of_month(year, 1, 0, 3),  # Dia de Martin Luther King Jr.
        _nth_weekday_of_month(year, 2, 0, 3),  # Dia de los Presidentes
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday_of_month(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),  # Dia de la Independencia
        _nth_weekday_of_month(year, 9, 0, 1),  # Labor Day
        _nth_weekday_of_month(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),  # Navidad
    }
    if year >= 2022:  # feriado federal desde 2021, primer anio bursatil 2022
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    return holidays


def _is_nyse_holiday(d: date) -> bool:
    # Se incluye el anio SIGUIENTE porque el unico feriado que puede
    # "correrse" a un anio distinto del suyo es Ano Nuevo de ese anio
    # siguiente observado el 31 de diciembre de este anio (cuando el 1/1 cae
    # sabado): _nyse_holidays(d.year + 1) es quien genera esa fecha de 31 de
    # diciembre, no _nyse_holidays(d.year).
    return d in _nyse_holidays(d.year) or d in _nyse_holidays(d.year + 1)


class RulesEngine:
    """Evalua una orden contra las reglas de riesgo configuradas.

    Una orden se aprueba solo si NINGUNA regla la rechaza. Si se aprueba pero su
    valor supera manual_approval_threshold_usd, queda marcada como pendiente de
    aprobacion manual aunque el modo de ejecucion sea automatico.
    """

    def __init__(self, config: RulesConfig):
        self.config = config

    def reload(self, config: RulesConfig) -> None:
        self.config = config

    def _within_trading_hours(self, now: datetime | None = None) -> bool:
        if self.config.allow_extended_hours:
            return True
        tz = ZoneInfo(self.config.trading_hours_timezone)
        current = now or datetime.now(tz)
        # Un `now` sin tzinfo se interpreta como ya expresado en `tz` (ej. la
        # hora de pared de Nueva York que pasaria un test o un futuro
        # caller), en vez de con .astimezone(tz), que a un datetime naive lo
        # asume erroneamente en la hora LOCAL DEL SISTEMA (en un contenedor
        # productivo, casi siempre UTC, no la zona configurada aca).
        current = current.replace(tzinfo=tz) if current.tzinfo is None else current.astimezone(tz)
        if current.weekday() >= 5:  # sabado=5, domingo=6: mercado cerrado
            return False
        if _is_nyse_holiday(current.date()):
            return False
        start = _parse_hhmm(self.config.trading_hours_start)
        end = _parse_hhmm(self.config.trading_hours_end)
        # Extremo inferior inclusivo, superior exclusivo: una orden a las 16:00:00
        # exactas llegaria al broker fuera de sesion (cierre 16:00 ET).
        return start <= current.time() < end

    def evaluate(
        self,
        order: OrderRequest,
        account: AccountSummary,
        current_position_qty: float,
        reference_price: float,
        trades_today: int,
        halted: bool,
        order_sector: str | None = None,
        sector_exposure_usd: dict[str, float] | None = None,
        max_concurrent_positions_per_sector: int | None = None,
        sector_position_count: dict[str, int] | None = None,
        total_position_value_usd: float | None = None,
        open_portfolio_risk_usd: float | None = None,
        trades_today_for_fund: int | None = None,
        effective_max_order_value_usd: float | None = None,
    ) -> OrderDecision:
        """`order_sector` y `sector_exposure_usd` son opcionales y se ignoran
        si `order_sector` es None: sin sector conocido para el simbolo no hay
        forma de evaluar el limite de concentracion, y se prefiere no
        bloquear la orden por falta de un dato secundario (mismo criterio que
        otros filtros best-effort del codebase, ej. earnings/near-high del
        screener). `sector_exposure_usd` debe excluir la posicion actual del
        propio simbolo de la orden si correspondiera evitar contarla dos
        veces junto con `resulting_value` (queda a cargo del llamador, que
        tiene visibilidad del portfolio completo).

        `max_concurrent_positions_per_sector`/`sector_position_count`: limite
        de CANTIDAD de posiciones distintas abiertas en un mismo sector (no de
        exposicion en USD, eso ya lo cubre max_sector_concentration_pct arriba).
        Antes de este chequeo, este limite (ScreenerConfig.
        max_concurrent_positions_per_sector) solo se aplicaba en el backtest
        (ver cap_concurrent_positions en backtest.py), nunca en el motor de
        auto-trading en vivo -- una discrepancia real entre lo que el backtest
        validaba y lo que la cuenta en vivo permitia hacer. `sector_position_
        count` debe excluir la posicion actual del propio simbolo de la orden
        (mismo criterio que sector_exposure_usd); None o 0/falsy en
        max_concurrent_positions_per_sector desactiva el chequeo. Solo aplica
        a compras que abren una posicion NUEVA (current_position_qty == 0):
        agregar a una posicion ya abierta no aumenta la cantidad de simbolos
        distintos ocupados en ese sector.

        `total_position_value_usd`: valor USD de TODAS las posiciones
        actuales, excluyendo el propio simbolo de la orden (mismo criterio
        que sector_exposure_usd), para chequear max_total_exposure_pct
        (exposicion bruta de TODA la cartera, no de un sector). None
        desactiva el chequeo (sin dato, no se rechaza).

        `open_portfolio_risk_usd`: suma en USD del riesgo (entrada - stop) x
        cantidad de todas las posiciones abiertas con stop-loss, excluyendo
        el propio simbolo de la orden, para chequear max_portfolio_heat_pct
        (cuanto se perderia en total si TODOS los stops se tocaran a la vez).
        Solo aplica a compras con stop-loss definido (sin stop no hay riesgo
        acotado que sumar). None desactiva el chequeo.

        `trades_today_for_fund`: cantidad de operaciones ya ejecutadas HOY
        para el fondo de `order.fund_id` (ver AuditLog.count_trades_today),
        para chequear max_trades_per_day_per_fund -- un cupo diario adicional
        POR fondo, distinto de `trades_today`/max_trades_per_day (el cupo
        global, que sigue contando todas las ordenes sin distinguir fondo).
        Solo aplica si la orden esta atada a un fondo (order.fund_id no es
        None); ordenes de la cuenta general no tienen fondo del que contar."""
        violations: list[RuleViolation] = []

        if halted:
            violations.append(RuleViolation(
                rule="halted",
                message="El trading esta pausado (kill switch activo).",
            ))

        if self.config.symbol_whitelist:
            if order.symbol not in self.config.symbol_whitelist:
                violations.append(RuleViolation(
                    rule="symbol_whitelist",
                    message=f"{order.symbol} no esta en la lista blanca de simbolos permitidos.",
                ))
        else:
            violations.append(RuleViolation(
                rule="symbol_whitelist",
                message=(
                    "La lista blanca de simbolos esta vacia: por seguridad no se "
                    "permite ningun simbolo hasta agregarlo explicitamente."
                ),
            ))

        price = order.limit_price or reference_price
        estimated_value = price * order.quantity

        order_ceiling = effective_max_order_value_usd or self.config.max_order_value_usd
        if estimated_value > order_ceiling:
            violations.append(RuleViolation(
                rule="max_order_value_usd",
                message=(
                    f"Valor estimado ${estimated_value:,.2f} excede el maximo por "
                    f"orden (${order_ceiling:,.2f})."
                ),
            ))

        signed_qty = order.quantity if order.side == Side.BUY else -order.quantity
        resulting_qty = current_position_qty + signed_qty

        if not self.config.allow_short_selling and resulting_qty < 0:
            violations.append(RuleViolation(
                rule="allow_short_selling",
                message=(
                    f"La orden dejaria una posicion corta ({resulting_qty:g}) en "
                    f"{order.symbol}. El short selling esta deshabilitado "
                    "(allow_short_selling=false)."
                ),
            ))

        if account.net_liquidation > 0:
            resulting_value = abs(resulting_qty) * price
            position_pct = (resulting_value / account.net_liquidation) * 100
            if position_pct > self.config.max_position_pct_of_equity:
                violations.append(RuleViolation(
                    rule="max_position_pct_of_equity",
                    message=(
                        f"La posicion resultante en {order.symbol} seria "
                        f"{position_pct:.1f}% del equity (maximo "
                        f"{self.config.max_position_pct_of_equity}%)."
                    ),
                ))

            if order_sector:
                other_sector_value = (sector_exposure_usd or {}).get(order_sector, 0.0)
                resulting_sector_value = other_sector_value + resulting_value
                sector_pct = (resulting_sector_value / account.net_liquidation) * 100
                if sector_pct > self.config.max_sector_concentration_pct:
                    violations.append(RuleViolation(
                        rule="max_sector_concentration_pct",
                        message=(
                            f"La exposicion combinada al sector {order_sector} seria "
                            f"{sector_pct:.1f}% del equity (maximo "
                            f"{self.config.max_sector_concentration_pct}%)."
                        ),
                    ))

            if total_position_value_usd is not None:
                resulting_total_value = total_position_value_usd + resulting_value
                total_exposure_pct = (resulting_total_value / account.net_liquidation) * 100
                if total_exposure_pct > self.config.max_total_exposure_pct:
                    violations.append(RuleViolation(
                        rule="max_total_exposure_pct",
                        message=(
                            f"La exposicion bruta total de la cartera seria "
                            f"{total_exposure_pct:.1f}% del equity (maximo "
                            f"{self.config.max_total_exposure_pct}%)."
                        ),
                    ))

            if (
                open_portfolio_risk_usd is not None
                and order.side == Side.BUY
                and order.stop_loss_price
            ):
                this_order_risk_usd = max(0.0, price - order.stop_loss_price) * order.quantity
                resulting_risk_usd = open_portfolio_risk_usd + this_order_risk_usd
                portfolio_heat_pct = (resulting_risk_usd / account.net_liquidation) * 100
                if portfolio_heat_pct > self.config.max_portfolio_heat_pct:
                    violations.append(RuleViolation(
                        rule="max_portfolio_heat_pct",
                        message=(
                            f"Si se tocaran todos los stops abiertos (incluido este), la "
                            f"perdida combinada seria {portfolio_heat_pct:.1f}% del equity "
                            f"(maximo {self.config.max_portfolio_heat_pct}%)."
                        ),
                    ))

        if (
            order.side == Side.BUY
            and order_sector
            and current_position_qty == 0
            and max_concurrent_positions_per_sector
        ):
            current_count = (sector_position_count or {}).get(order_sector, 0)
            if current_count >= max_concurrent_positions_per_sector:
                violations.append(RuleViolation(
                    rule="max_concurrent_positions_per_sector",
                    message=(
                        f"El sector {order_sector} ya tiene {current_count} posicion(es) "
                        f"abierta(s) (maximo {max_concurrent_positions_per_sector}): no se "
                        "abren mas hasta cerrar alguna."
                    ),
                ))

        if not account.pnl_data_available:
            # Sin dato real de PnL diario (recien conectado, antes del primer
            # callback de reqPnL, o cuenta sin NetLiquidation), daily_pnl_pct
            # default queda en 0.0 -- eso NO significa "sin perdida hoy", solo
            # que no hay dato. Aprobar ordenes en este estado fallaria ABIERTO
            # justo cuando menos se puede confiar en el dato: si la cuenta ya
            # esta por debajo del limite de perdida diaria pero el callback de
            # IBKR todavia no llego, el chequeo de abajo nunca lo veria.
            violations.append(RuleViolation(
                rule="pnl_data_available",
                message=(
                    "Todavia no hay dato real de PnL diario de IBKR disponible "
                    "(recien conectado o cuenta sin datos). Por seguridad, las "
                    "ordenes quedan bloqueadas hasta que el dato este disponible."
                ),
            ))
        elif account.daily_pnl_pct <= -abs(self.config.daily_loss_limit_pct):
            violations.append(RuleViolation(
                rule="daily_loss_limit_pct",
                message=(
                    f"Perdida diaria {account.daily_pnl_pct:.2f}% alcanzo el limite "
                    f"(-{self.config.daily_loss_limit_pct}%). Trading detenido por hoy."
                ),
            ))

        if trades_today >= self.config.max_trades_per_day:
            violations.append(RuleViolation(
                rule="max_trades_per_day",
                message=f"Ya se alcanzo el maximo de {self.config.max_trades_per_day} operaciones hoy.",
            ))

        if (
            order.fund_id is not None
            and self.config.max_trades_per_day_per_fund is not None
            and trades_today_for_fund is not None
            and trades_today_for_fund >= self.config.max_trades_per_day_per_fund
        ):
            violations.append(RuleViolation(
                rule="max_trades_per_day_per_fund",
                message=(
                    f"Este fondo ya alcanzo su maximo de "
                    f"{self.config.max_trades_per_day_per_fund} operaciones hoy."
                ),
            ))

        if order.side == Side.BUY and self.config.require_stop_loss_on_buy:
            if not order.stop_loss_price:
                violations.append(RuleViolation(
                    rule="require_stop_loss_on_buy",
                    message="Toda compra debe incluir un precio de stop-loss.",
                ))
            else:
                stop_pct = (price - order.stop_loss_price) / price * 100
                if stop_pct <= 0:
                    violations.append(RuleViolation(
                        rule="require_stop_loss_on_buy",
                        message="El stop-loss debe estar por debajo del precio de entrada.",
                    ))
                elif stop_pct > self.config.max_stop_loss_pct:
                    violations.append(RuleViolation(
                        rule="max_stop_loss_pct",
                        message=(
                            f"El stop-loss implica {stop_pct:.1f}% de riesgo, mayor "
                            f"al maximo permitido ({self.config.max_stop_loss_pct}%)."
                        ),
                    ))

        if not self._within_trading_hours():
            violations.append(RuleViolation(
                rule="trading_hours",
                message=(
                    f"Fuera del horario de trading permitido "
                    f"({self.config.trading_hours_start}-{self.config.trading_hours_end} "
                    f"{self.config.trading_hours_timezone})."
                ),
            ))

        approved = len(violations) == 0
        requires_manual_approval = approved and estimated_value > self.config.manual_approval_threshold_usd

        return OrderDecision(
            approved=approved,
            requires_manual_approval=requires_manual_approval,
            violations=violations,
            estimated_value_usd=estimated_value,
        )

    def suggested_quantity(
        self,
        equity: float,
        current_position_qty: float,
        entry_price: float,
        stop_loss_price: float,
        score: float | None = None,
    ) -> PositionSizeSuggestion:
        """Sugiere una cantidad para una compra en base al riesgo, no a un monto
        fijo arbitrario: el tamano se calcula para que, si se toca el
        stop-loss, la perdida no supere risk_per_trade_pct del equity. Se
        recorta ademas por max_position_pct_of_equity y max_order_value_usd
        para no sugerir una cantidad que el motor de reglas rechazaria de
        todas formas al enviarla. Es solo una sugerencia editable, no se
        aplica sola.

        `equity` es el capital contra el que se dimensiona: el equity de toda
        la cuenta de IBKR para ordenes sin fondo asociado, o el
        equity_estimate() de un fondo puntual cuando la orden esta atada a
        uno (ver _try_auto_trade_entry y order_size_suggestion en main.py) --
        asi un fondo chico no recibe una posicion sizeada como si tuviera
        detras el capital de toda la cuenta.

        `score` (0-100, el mismo score de SignalResult/scoring.py) ajusta el
        presupuesto de riesgo por conviccion: a mayor score, mayor tamano de
        posicion dentro del riesgo tolerado, sin tocar los topes duros
        (max_position_pct_of_equity, max_order_value_usd). score=50 (punto
        medio de la escala 0-100) deja el sizing identico al de antes de este
        ajuste (multiplicador 1.0); score=100 lo sube a 1.5x, score=0 lo baja
        a 0.5x. None (default) tampoco ajusta nada -- solo se pasa el score
        desde las señales del screener, que ya vienen en esa escala.
        """
        risk_per_share = entry_price - stop_loss_price
        if entry_price <= 0 or equity <= 0 or risk_per_share <= 0:
            return PositionSizeSuggestion(quantity=0.0, risk_usd=0.0, limited_by=None)

        # conviction_multiplier escala tanto el risk budget como el tope de
        # posición (max_position_pct y max_order_value_usd): sin esto, cuando
        # max_pos es el binding constraint —lo normal— el score no tiene efecto
        # real sobre el tamaño de la posición. Rango: 0.5× (score=0) → 1.5×
        # (score=100), neutro en score=50. Señal excelente (score=85):
        # 0.5+85/100=1.35× → 35% más capital que una señal media.
        conviction_multiplier = 1.0 if score is None else min(1.5, max(0.5, 0.5 + score / 100))
        risk_budget_usd = equity * self.config.risk_per_trade_pct / 100 * conviction_multiplier
        qty_by_risk = risk_budget_usd / risk_per_share

        effective_max_pos_pct = self.config.max_position_pct_of_equity * conviction_multiplier
        max_position_value = equity * effective_max_pos_pct / 100
        remaining_value = max(0.0, max_position_value - current_position_qty * entry_price)
        qty_by_position_pct = remaining_value / entry_price

        effective_max_order_usd = self.config.max_order_value_usd * conviction_multiplier
        qty_by_order_value = effective_max_order_usd / entry_price

        qty_raw, limited_by = min(
            (qty_by_risk, None),
            (qty_by_position_pct, "max_position_pct_of_equity"),
            (qty_by_order_value, "max_order_value_usd"),
            key=lambda pair: pair[0],
        )
        qty = max(0.0, float(math.floor(qty_raw)))
        return PositionSizeSuggestion(
            quantity=qty,
            risk_usd=round(qty * risk_per_share, 2),
            limited_by=limited_by,
        )
