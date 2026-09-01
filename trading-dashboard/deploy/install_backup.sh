#!/usr/bin/env bash
# install_backup.sh — Instala el cron job de backup como root (ejecutar UNA vez).
#
# Uso:
#   ssh root@100.92.236.44
#   bash /opt/panel/trading-dashboard/deploy/install_backup.sh

set -euo pipefail

SCRIPT_SRC="/opt/panel/trading-dashboard/deploy/backup.sh"
CRON_DEST="/etc/cron.daily/trading-backup"
BACKUP_ROOT="/opt/panel/backups"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: este script debe correr como root (uid=$(id -u))"
    exit 1
fi

# Instalar en cron.daily
cp "$SCRIPT_SRC" "$CRON_DEST"
chmod 755 "$CRON_DEST"
chown root:root "$CRON_DEST"
echo "✓ Instalado en $CRON_DEST"

# Crear directorio de backups
mkdir -p "$BACKUP_ROOT"
echo "✓ Directorio $BACKUP_ROOT creado"

# Test inmediato
echo ""
echo "Ejecutando backup de prueba..."
"$CRON_DEST"

echo ""
echo "✓ Instalación completa. El backup corre diariamente (~06:25 AM)."
echo "  Snapshots en: $BACKUP_ROOT"
echo "  Logs: journalctl -t trading-backup"
