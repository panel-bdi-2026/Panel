from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Escribe `content` en `path` de forma atomica: primero a un archivo
    temporal en el mismo directorio (mismo filesystem, para que os.replace sea
    atomico) y recien despues lo renombra sobre el destino final.

    Sin esto, un crash a mitad de un write_text() comun (falta de espacio,
    kill -9, perdida de energia) deja el archivo truncado/corrupto. Para
    funds.json/state.json/rules.yaml/screener.yaml eso significa perder en
    silencio fondos, ordenes pendientes, el halt o la config de reglas: tanto
    load_state() como FundsStore.load() tratan un archivo corrupto como
    "estado vacio" en vez de fallar fuerte, justamente para poder arrancar el
    backend igual ante un problema de datos -- pero eso solo es seguro si la
    escritura en si es atomica.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
