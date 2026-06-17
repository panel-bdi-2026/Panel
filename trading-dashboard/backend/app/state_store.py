from __future__ import annotations

import json
from pathlib import Path


def load_state(path: Path) -> dict:
    """Lee el estado persistido (halted, mode, pending_orders). Si el archivo
    no existe o esta corrupto, arranca desde un estado vacio en vez de
    explotar -- es preferible perder el estado guardado a que el backend no
    pueda arrancar."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
