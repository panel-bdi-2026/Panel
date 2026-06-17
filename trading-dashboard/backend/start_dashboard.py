"""Lanzador simple del dashboard: prepara .env si falta, detecta la IP de la
red local y muestra como acceder desde otros dispositivos (ej. una tablet).

Pensado para gente sin experiencia tecnica: alcanza con ejecutar este
archivo (start_windows.bat lo hace por vos en Windows).
"""

from __future__ import annotations

import socket
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ENV_EXAMPLE_PATH = BASE_DIR / ".env.example"
PORT = 8000


def ensure_env_file() -> None:
    if ENV_PATH.exists():
        return
    if not ENV_EXAMPLE_PATH.exists():
        return
    content = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
    content = content.replace("API_KEY=cambia-esta-clave", "API_KEY=clave-de-prueba-cambiame")
    ENV_PATH.write_text(content, encoding="utf-8")
    print(f"Cree {ENV_PATH.name} con valores de prueba (modo paper, sin IBKR conectado).")


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def print_access_info(url: str) -> None:
    print("=" * 60)
    print("Dashboard disponible en:")
    print(f"  Desde esta laptop:                    http://localhost:{PORT}")
    print(f"  Desde tu tablet/celular (misma WiFi):  {url}")
    print("=" * 60)
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        print("Escanea este codigo con la camara de tu tablet:\n")
        qr.print_ascii(invert=True)
        print()
    except Exception:
        print("(Instala 'qrcode' para ver un codigo QR escaneable aqui: pip install qrcode)")
    print("=" * 60)
    print("Deja esta ventana abierta mientras uses el dashboard.")
    print("Para apagarlo: cerra esta ventana o presiona Ctrl+C.")
    print("=" * 60)


def main() -> None:
    ensure_env_file()
    ip = get_lan_ip()
    url = f"http://{ip}:{PORT}"
    print_access_info(url)

    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
