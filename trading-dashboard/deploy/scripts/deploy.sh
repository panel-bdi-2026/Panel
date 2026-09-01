#!/usr/bin/env bash
# deploy.sh — despliega la rama de trabajo en el droplet.
# Uso: bash deploy.sh [rama]
# Si no se pasa rama, usa la rama actual del repo.
# Requiere correr como root (o con sudo para systemctl).

set -euo pipefail

BRANCH="${1:-$(git -C /opt/panel rev-parse --abbrev-ref HEAD)}"
REPO=/opt/panel
BACKEND=$REPO/trading-dashboard/backend
SERVICE=trading-dashboard
SYSTEMD_SRC=$REPO/trading-dashboard/deploy/systemd/$SERVICE.service
SYSTEMD_DST=/etc/systemd/system/$SERVICE.service

echo "==> Rama: $BRANCH"

echo "==> git pull"
git -C "$REPO" fetch origin
git -C "$REPO" pull origin "$BRANCH"

echo "==> pip install"
"$BACKEND/.venv/bin/pip" install -r "$BACKEND/requirements.txt" --quiet

echo "==> permisos"
chown -R trading:trading "$BACKEND/app" "$BACKEND/requirements.txt" 2>/dev/null || true

# Actualizar el service file si cambió en el repo
if ! diff -q "$SYSTEMD_SRC" "$SYSTEMD_DST" > /dev/null 2>&1; then
    echo "==> service file actualizado, aplicando..."
    cp "$SYSTEMD_SRC" "$SYSTEMD_DST"
    systemctl daemon-reload
fi

echo "==> restart $SERVICE"
systemctl restart "$SERVICE"
sleep 3
systemctl status "$SERVICE" --no-pager | head -12

echo "==> listo"
