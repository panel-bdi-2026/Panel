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

    tempfile.mkstemp() crea el archivo temporal con permisos 0600 (solo el
    dueño puede leerlo) sin importar los permisos que tuviera el archivo
    destino, y os.replace() no los ajusta: cada guardado (cualquier PUT que
    persista config, no solo una edicion manual) volvia a dejar el archivo en
    0600, pisando en silencio un chmod mas permisivo que se hubiera aplicado
    antes (ej. group-read para que otro usuario del sistema pueda leerlo).
    Se preserva el modo del archivo destino si ya existia (respeta cualquier
    esquema de permisos que el administrador haya establecido a proposito);
    si es la primera vez que se crea, se usa 0644 (legible por cualquiera,
    escribible solo por el dueño) en vez del 0600 por defecto de mkstemp,
    mas razonable para un archivo de config."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = path.stat().st_mode & 0o777
    except OSError:
        existing_mode = 0o644
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.chmod(tmp_name, existing_mode)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())  # fuerza el contenido a disco antes del rename
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _fsync_dir(dir_path: Path) -> None:
    """fsync del directorio padre: el fsync del archivo solo garantiza su
    contenido en disco, no que el rename (que entrada del directorio apunta
    a que inode) sobreviva un corte de luz. Best-effort: si el filesystem no
    soporta fsync de directorios, el peor caso es volver al contenido viejo
    tras un crash, no corromper nada."""
    try:
        fd = os.open(dir_path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass
