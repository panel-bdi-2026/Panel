#!/usr/bin/env bash
# backup.sh — Backup diario de archivos críticos del trading dashboard.
#
# Archivos respaldados:
#   funds.json          Estado de fondos y posiciones (pérdida = irrecuperable)
#   rules.yaml          Configuración de riesgo
#   screener_config     Parámetros del screener / estrategias
#   audit.db            Historial de operaciones y eventos
#
# Instalación (como root, una sola vez):
#   cp /opt/panel/trading-dashboard/deploy/backup.sh /etc/cron.daily/trading-backup
#   chmod +x /etc/cron.daily/trading-backup
#   /etc/cron.daily/trading-backup   # test inmediato
#
# El cron diario (/etc/cron.daily/) corre como root alrededor de las 6:25 AM
# en Debian/Ubuntu. Para cambiar el horario: editar /etc/crontab.
# Retención: 30 días de snapshots en /opt/panel/backups/.

set -euo pipefail

BACKEND="/opt/panel/trading-dashboard/backend"
BACKUP_ROOT="/opt/panel/backups"
PYTHON="$BACKEND/.venv/bin/python"
LOG_TAG="trading-backup"
TIMESTAMP=$(date +%Y-%m-%d)

log()  { logger -t "$LOG_TAG" "$*"; echo "[$(date +%H:%M:%S)] $*"; }
warn() { logger -t "$LOG_TAG" "WARN: $*"; echo "[$(date +%H:%M:%S)] WARN: $*" >&2; }

# ── Crear directorio del snapshot ───────────────────────────────────────────
DEST="$BACKUP_ROOT/$TIMESTAMP"
# Si ya existe un backup de hoy (ej: se corrió manualmente) lo sobreescribimos
mkdir -p "$DEST"
log "Backup iniciado → $DEST"

# ── Archivos de texto (JSON / YAML) ─────────────────────────────────────────
_copy() {
    local src="$1" name="$2"
    if [ -f "$src" ]; then
        cp "$src" "$DEST/$name"
        log "OK: $name ($(du -sh "$DEST/$name" | cut -f1))"
    else
        warn "$name no encontrado en $src"
    fi
}

_copy "$BACKEND/funds.json"                   "funds.json"
_copy "$BACKEND/rules.yaml"                   "rules.yaml"
_copy "$BACKEND/data/screener_config.json"    "screener_config.json"
_copy "$BACKEND/state.json"                   "state.json"

# ── SQLite — backup consistente vía API de Python ───────────────────────────
# cp de audit.db mientras el servicio escribe puede producir un archivo
# corrupto (WAL no flushado). sqlite3.Connection.backup() genera una copia
# limpia y consistent incluso con escrituras concurrentes en curso.
AUDIT_SRC="$BACKEND/audit.db"
if [ -f "$AUDIT_SRC" ]; then
    "$PYTHON" - "$AUDIT_SRC" "$DEST/audit.db" << 'PYEOF'
import sqlite3, sys, os
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PYEOF
    log "OK: audit.db ($(du -sh "$DEST/audit.db" | cut -f1), backup consistente)"
else
    warn "audit.db no encontrado en $AUDIT_SRC"
fi

STATE_SRC="$BACKEND/state.db"
if [ -f "$STATE_SRC" ]; then
    "$PYTHON" - "$STATE_SRC" "$DEST/state.db" << 'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)
PYEOF
    log "OK: state.db ($(du -sh "$DEST/state.db" | cut -f1), backup consistente)"
else
    warn "state.db no encontrado en $STATE_SRC (normal en primer deploy)"
fi

# ── Purgar snapshots más viejos de 30 días ──────────────────────────────────
PURGED=0
while IFS= read -r old_dir; do
    rm -rf "$old_dir"
    PURGED=$((PURGED + 1))
done < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +30)
[ "$PURGED" -gt 0 ] && log "Purgados $PURGED snapshots con más de 30 días"

# ── Resumen ──────────────────────────────────────────────────────────────────
TOTAL=$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d | wc -l)
SIZE=$(du -sh "$BACKUP_ROOT" | cut -f1)
log "Completado. $TOTAL snapshots en disco, $SIZE total."
