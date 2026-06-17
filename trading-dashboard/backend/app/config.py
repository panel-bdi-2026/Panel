from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings:
    def __init__(self) -> None:
        self.trading_mode = os.getenv("TRADING_MODE", "paper").strip().lower()
        self.live_confirm = os.getenv("LIVE_CONFIRM", "")

        self.ib_host = os.getenv("IB_HOST", "127.0.0.1")
        self.ib_port = int(os.getenv("IB_PORT", "7497"))  # 7497 = TWS paper trading (default seguro)
        self.ib_client_id = int(os.getenv("IB_CLIENT_ID", "17"))

        # Puertos usados al cambiar de modo desde el boton del dashboard (sin
        # reiniciar el backend). TWS: 7497 paper / 7496 live. IB Gateway: 4002
        # paper / 4001 live. Si no se definen, se asume que IB_PORT ya es el
        # puerto correcto para el modo con el que arranca el backend.
        self.ib_port_paper = int(
            os.getenv("IB_PORT_PAPER", str(self.ib_port if self.trading_mode == "paper" else 7497))
        )
        live_port_env = os.getenv("IB_PORT_LIVE")
        self.ib_port_live = (
            int(live_port_env) if live_port_env else (self.ib_port if self.trading_mode == "live" else None)
        )

        self.api_key = os.getenv("API_KEY", "")

        self.rules_path = Path(os.getenv("RULES_PATH", str(BASE_DIR / "rules.yaml")))
        self.screener_path = Path(os.getenv("SCREENER_PATH", str(BASE_DIR / "screener.yaml")))
        self.audit_db_path = Path(os.getenv("AUDIT_DB_PATH", str(BASE_DIR / "audit.db")))
        self.state_path = Path(os.getenv("STATE_PATH", str(BASE_DIR / "state.json")))
        self.poll_interval_seconds = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))

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
