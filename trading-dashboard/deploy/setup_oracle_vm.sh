#!/usr/bin/env bash
# Setup inicial para correr el dashboard + IB Gateway 24/7 en una VM Ubuntu
# headless (pensado para una instancia ARM "Always Free" de Oracle Cloud,
# pero sirve igual en cualquier VPS Ubuntu x86_64/ARM). Correr una sola vez:
#
#   sudo bash setup_oracle_vm.sh
#
# Ver deploy/README.md para el resto de los pasos (IB Gateway/IBC, systemd,
# Tailscale).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/panel}"
APP_USER="${APP_USER:-trading}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Corre este script con sudo/root." >&2
  exit 1
fi

apt-get update
# libxtst6/libxrender1/libxi6: el AWT de Java (usado por IB Gateway) las carga
# en tiempo de ejecucion al dibujar bajo Xvfb; sin ellas IBC falla con
# java.lang.UnsatisfiedLinkError y exit code=1100 sin mas explicacion.
apt-get install -y python3-venv python3-pip git unzip curl ufw xvfb \
  libxtst6 libxrender1 libxi6

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "Usuario de sistema '$APP_USER' creado."
fi

mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Firewall: solo SSH desde la red publica. El puerto del dashboard (8000) se
# habilita pero atado a la interfaz tailscale0 -- nunca a la interfaz
# publica -- asi que el dashboard nunca queda expuesto a internet, solo a tu
# red privada de Tailscale.
ufw allow OpenSSH
ufw allow in on tailscale0 to any port 8000 proto tcp
ufw default deny incoming
ufw default allow outgoing
ufw --force enable

curl -fsSL https://tailscale.com/install.sh | sh

echo
echo "============================================================"
echo "Setup base listo. Falta, a mano (ver deploy/README.md):"
echo "  1. sudo tailscale up    (pide autenticarte en el navegador)"
echo "  2. Clonar/copiar este repo en $APP_DIR si todavia no lo hiciste"
echo "  3. Instalar IB Gateway + IBC (Paso 3 del README)"
echo "  4. Configurar .env del backend y habilitar los servicios"
echo "     systemd (Pasos 4 y 5 del README)"
echo "============================================================"
