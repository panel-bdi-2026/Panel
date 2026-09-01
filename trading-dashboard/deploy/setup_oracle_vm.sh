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
# fail2ban + unattended-upgrades: mitigan el patron mas comun de compromiso de
# una VM expuesta -- scanning masivo de internet contra el puerto 22 probando
# credenciales (botnets estilo Mirai) y CVEs sin parchear.
apt-get install -y python3-venv python3-pip git unzip curl ufw xvfb \
  libxtst6 libxrender1 libxi6 fail2ban unattended-upgrades

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
  echo "Usuario de sistema '$APP_USER' creado."
fi

mkdir -p "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# Firewall: SSH publico queda con rate-limiting (ufw limit, no allow) para
# frenar brute-force/scanning masivo, y ya se deja lista la ruta por
# Tailscale para poder cerrar el SSH publico del todo despues (ver Paso 2.5
# del README) sin tener que tocar este script de nuevo. El puerto del
# dashboard (8000) solo se habilita atado a la interfaz tailscale0 -- nunca a
# la interfaz publica -- asi que el dashboard nunca queda expuesto a
# internet, solo a tu red privada de Tailscale.
ufw limit OpenSSH
ufw allow in on tailscale0 to any port 22 proto tcp
ufw allow in on tailscale0 to any port 8000 proto tcp
ufw default deny incoming
ufw default allow outgoing
ufw --force enable

# fail2ban: banea IPs que fallan el login de SSH repetidas veces. El jail de
# sshd viene deshabilitado por defecto en el paquete de Ubuntu/Debian.
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled = true
EOF
systemctl enable --now fail2ban

# unattended-upgrades: instala automaticamente los parches de seguridad del
# sistema sin esperar a que alguien entre a actualizar a mano.
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# SSH solo por llave: se desactiva el login por contrasena, pero solo si ya
# hay al menos una llave autorizada cargada -- si no, se deja como esta para
# no arriesgarse a dejarte afuera del servidor antes de tiempo.
SSH_KEY_FOUND=false
for f in /root/.ssh/authorized_keys ${SUDO_USER:+/home/$SUDO_USER/.ssh/authorized_keys}; do
  if [ -s "$f" ] 2>/dev/null; then
    SSH_KEY_FOUND=true
  fi
done

if [ "$SSH_KEY_FOUND" = true ]; then
  sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
  sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
  systemctl reload ssh || systemctl reload sshd
  echo "SSH: login por contrasena deshabilitado (se encontro una llave autorizada)."
else
  echo "AVISO: no se encontro ninguna llave SSH autorizada -- se deja el login por" >&2
  echo "contrasena activo para no dejarte afuera del servidor. Configura una llave y" >&2
  echo "despues corre a mano (ver Paso 2.5 del README): PasswordAuthentication no /" >&2
  echo "PermitRootLogin prohibit-password en /etc/ssh/sshd_config, despues" >&2
  echo "'systemctl reload ssh'." >&2
fi

curl -fsSL https://tailscale.com/install.sh | sh

echo
echo "============================================================"
echo "Setup base listo. Falta, a mano (ver deploy/README.md):"
echo "  1. sudo tailscale up    (pide autenticarte en el navegador)"
echo "  2. Una vez confirmado que Tailscale funciona: cerrar el SSH"
echo "     publico del todo (Paso 2.5 del README)"
echo "  3. Clonar/copiar este repo en $APP_DIR si todavia no lo hiciste"
echo "  4. Instalar IB Gateway + IBC (Paso 3 del README)"
echo "  5. Configurar .env del backend y habilitar los servicios"
echo "     systemd (Pasos 4 y 5 del README)"
echo "============================================================"
