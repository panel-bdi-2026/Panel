from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _int_env(name: str, raw: str) -> int:
    """Convierte una variable de entorno a int con un error claro: sin esto,
    un valor mal escrito (ej. IB_PORT="74977" con una letra de mas, o vacio)
    tira un ValueError crudo de int() al importar app.config, con un
    traceback que no dice cual variable de entorno fue ni que se esperaba."""
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} invalido: {raw!r}. Debe ser un numero entero.") from None


def _float_env(name: str, raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"{name} invalido: {raw!r}. Debe ser un numero.") from None


class Settings:
    def __init__(self) -> None:
        self.trading_mode = os.getenv("TRADING_MODE", "paper").strip().lower()
        self.live_confirm = os.getenv("LIVE_CONFIRM", "")

        self.ib_host = os.getenv("IB_HOST", "127.0.0.1")
        # 7497 = TWS paper trading (default seguro)
        self.ib_port = _int_env("IB_PORT", os.getenv("IB_PORT", "7497"))
        self.ib_client_id = _int_env("IB_CLIENT_ID", os.getenv("IB_CLIENT_ID", "17"))
        # Tipo de datos de mercado de IBKR (reqMarketDataType): 1=real-time,
        # 2=frozen, 3=delayed, 4=delayed-frozen. 3 (delayed, ~15-20 min) es el
        # default porque funciona sin ninguna suscripcion de datos activa. Si
        # la cuenta (incluida la paper, que suele heredar los mismos
        # entitlements que la cuenta real vinculada) tiene datos en tiempo
        # real habilitados, cambiar a 1 aca sin tocar codigo: el pricing de
        # auto-trading (limit price, sizing) hoy se calcula sobre un precio
        # que puede tener hasta 15-20 minutos de antiguedad, lo cual pega mas
        # fuerte en Momentum, cuya tesis depende de capturar movimiento
        # reciente.
        self.ib_market_data_type = _int_env("IB_MARKET_DATA_TYPE", os.getenv("IB_MARKET_DATA_TYPE", "3"))

        # Puertos usados al cambiar de modo desde el boton del dashboard (sin
        # reiniciar el backend). TWS: 7497 paper / 7496 live. IB Gateway: 4002
        # paper / 4001 live. Si no se definen, se asume que IB_PORT ya es el
        # puerto correcto para el modo con el que arranca el backend.
        self.ib_port_paper = _int_env(
            "IB_PORT_PAPER",
            os.getenv("IB_PORT_PAPER", str(self.ib_port if self.trading_mode == "paper" else 7497)),
        )
        live_port_env = os.getenv("IB_PORT_LIVE")
        self.ib_port_live = (
            _int_env("IB_PORT_LIVE", live_port_env)
            if live_port_env
            else (self.ib_port if self.trading_mode == "live" else None)
        )

        self.api_key = os.getenv("API_KEY", "")
        # Para app/news_sentiment.py (clasificacion de sentimiento de noticias
        # via Claude Haiku 4.5): a diferencia de api_key, no es requerido en
        # _validate() porque la feature es opt-in (news_sentiment_enabled,
        # apagado por defecto en ScreenerConfig) -- sin esta key, simplemente
        # no se calcula sentimiento (fail-safe, ver get_news_sentiment), no
        # hace falta tirar abajo todo el backend por una funcionalidad que el
        # usuario ni siquiera prendio.
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.tiingo_api_key = os.getenv("TIINGO_API_KEY", "")

        # Origenes permitidos para CORS. Vacio por defecto: el frontend se sirve
        # desde el mismo backend (mismo origen), asi que no necesita CORS, y
        # dejar "*" abierto permitia que cualquier web hiciera requests a los
        # endpoints (vector de DNS rebinding contra un backend en la LAN). Si
        # servis el frontend aparte, lista los origenes separados por coma.
        self.allowed_origins = [
            o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()
        ]

        self.rules_path = Path(os.getenv("RULES_PATH", str(BASE_DIR / "rules.yaml")))
        self.screener_path = Path(os.getenv("SCREENER_PATH", str(BASE_DIR / "screener.yaml")))
        self.audit_db_path = Path(os.getenv("AUDIT_DB_PATH", str(BASE_DIR / "audit.db")))
        self.state_path = Path(os.getenv("STATE_PATH", str(BASE_DIR / "state.json")))
        self.funds_path = Path(os.getenv("FUNDS_PATH", str(BASE_DIR / "funds.json")))
        self.poll_interval_seconds = _float_env("POLL_INTERVAL_SECONDS", os.getenv("POLL_INTERVAL_SECONDS", "5"))

        # Alertas por email (opcionales — si no se configuran, las alertas
        # se deshabilitan en silencio sin tirar error al arrancar).
        #
        # Backend recomendado (DigitalOcean bloquea SMTP):
        #   RESEND_API_KEY=re_...   (resend.com, gratis hasta 3000/mes)
        #   ALERT_EMAIL_FROM=Trading Dashboard <onboarding@resend.dev>
        #   ALERT_EMAIL_TO=tu@email.com
        #
        # Backend alternativo (SMTP, si el proveedor no bloquea el puerto):
        #   SMTP_HOST=smtp.gmail.com SMTP_PORT=587
        #   SMTP_USER=tu@gmail.com SMTP_PASSWORD=<app-password>
        self.resend_api_key = os.getenv("RESEND_API_KEY", "")
        self.alert_email_from = os.getenv("ALERT_EMAIL_FROM", "")
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = _int_env("SMTP_PORT", os.getenv("SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SMTP_USER", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.alert_email_to = os.getenv("ALERT_EMAIL_TO", "")
        # Intervalo entre chequeos de salud del sistema (segundos). 300 = 5 min.
        self.health_alert_interval_seconds = _int_env(
            "HEALTH_ALERT_INTERVAL_SECONDS",
            os.getenv("HEALTH_ALERT_INTERVAL_SECONDS", "300"),
        )

        self._validate()

    def _validate(self) -> None:
        if self.trading_mode not in ("paper", "live"):
            raise RuntimeError(
                f"TRADING_MODE invalido: {self.trading_mode!r}. Usa 'paper' o 'live'."
            )
        if self.trading_mode == "live" and self.live_confirm != "I-UNDERSTAND-THIS-USES-REAL-MONEY":
            raise RuntimeError(
                "TRADING_MODE=live requiere LIVE_CONFIRM=I-UNDERSTAND-THIS-USES-REAL-MONEY "
                "en el .env. Esta confirmacion explicita existe para evitar activar "
                "dinero real por accidente."
            )
        if not self.api_key:
            raise RuntimeError("Define API_KEY en el .env antes de arrancar el backend.")


settings = Settings()
