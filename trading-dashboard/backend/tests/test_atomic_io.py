import os

import pytest

from app import atomic_io


def test_atomic_write_creates_file_with_content(tmp_path):
    target = tmp_path / "data.json"
    atomic_io.atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_atomic_write_overwrites_existing_file_completely(tmp_path):
    target = tmp_path / "data.json"
    target.write_text("contenido viejo mas largo que el nuevo", encoding="utf-8")
    atomic_io.atomic_write_text(target, "nuevo")
    assert target.read_text(encoding="utf-8") == "nuevo"


def test_atomic_write_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "data.json"
    atomic_io.atomic_write_text(target, "contenido")
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_atomic_write_preserves_original_file_if_write_fails(tmp_path, monkeypatch):
    """Si la escritura al archivo temporal falla a mitad de camino (ej. disco
    lleno), el archivo original tiene que quedar intacto: nunca se llega a
    hacer os.replace() sobre el. Tambien no debe quedar un .tmp huerfano."""
    target = tmp_path / "data.json"
    target.write_text("original", encoding="utf-8")

    class BoomError(Exception):
        pass

    def boom_fdopen(fd, *args, **kwargs):
        os.close(fd)
        raise BoomError("disco lleno")

    monkeypatch.setattr(atomic_io.os, "fdopen", boom_fdopen)
    with pytest.raises(BoomError):
        atomic_io.atomic_write_text(target, "contenido nuevo que nunca se escribe")

    assert target.read_text(encoding="utf-8") == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_atomic_write_preserves_existing_file_permissions(tmp_path):
    # tempfile.mkstemp() crea el archivo temporal en 0600 sin importar los
    # permisos del destino, y os.replace() no los ajusta -- sin este fix,
    # cada guardado volvia a dejar un archivo group-readable (0664, ej. para
    # que otro usuario del sistema en el mismo grupo pueda leerlo) en 0600,
    # pisando en silencio ese permiso mas amplio en cada escritura posterior.
    target = tmp_path / "data.json"
    target.write_text("original", encoding="utf-8")
    os.chmod(target, 0o664)

    atomic_io.atomic_write_text(target, "nuevo contenido")

    assert target.read_text(encoding="utf-8") == "nuevo contenido"
    assert oct(target.stat().st_mode & 0o777) == oct(0o664)


def test_atomic_write_uses_0644_default_for_new_file(tmp_path):
    # Un archivo que nunca existio antes no tiene permisos previos que
    # preservar: usa 0644 (legible por cualquiera) en vez del 0600 por
    # defecto de mkstemp, mas razonable para un archivo de config nuevo.
    target = tmp_path / "data.json"
    atomic_io.atomic_write_text(target, "contenido")
    assert oct(target.stat().st_mode & 0o777) == oct(0o644)


def test_atomic_write_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "nested" / "deeper" / "data.json"
    atomic_io.atomic_write_text(target, '{"a": 1}')
    assert target.read_text(encoding="utf-8") == '{"a": 1}'


def test_atomic_write_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    fsynced_fds = []

    real_fsync = os.fsync

    def spy_fsync(fd):
        fsynced_fds.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(atomic_io.os, "fsync", spy_fsync)
    atomic_io.atomic_write_text(target, "contenido")

    assert target.read_text(encoding="utf-8") == "contenido"
    # Se espera un fsync del archivo y otro del directorio padre.
    assert len(fsynced_fds) == 2
