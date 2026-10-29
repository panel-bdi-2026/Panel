#!/usr/bin/env bash
# Junta en un solo reporte el estado del servicio ibgateway (systemd, journal,
# procesos, permisos, config.ini sin password y el ultimo log de diagnostico
# de IBC) para no tener que ir comando por comando al depurar un arranque
# fallido. Pensado para correr en el servidor donde corre ibgateway.service.
set -uo pipefail

IBC_DIR="${IBC_DIR:-/opt/ibc}"
TWS_USER_HOME="${TWS_USER_HOME:-/home/trading}"

section() { printf '\n===== %s =====\n' "$1"; }

section "systemctl status ibgateway"
systemctl status ibgateway --no-pager -l || true

section "journalctl -u ibgateway (ultimas 80 lineas)"
journalctl -u ibgateway -n 80 --no-pager || true

section "Procesos Xvfb / java"
pgrep -fa 'Xvfb|java' || echo "(ninguno corriendo)"

section "Sockets X11 activos en /tmp/.X11-unix"
ls -la /tmp/.X11-unix/ 2>/dev/null || echo "(no hay ningun socket X11)"

section "config.ini (passwords enmascaradas)"
if [[ -f "$IBC_DIR/config.ini" ]]; then
  sed -E 's/^(Ib[A-Za-z]*[Pp]assword|.*Password)=.*/\1=***/' "$IBC_DIR/config.ini"
else
  echo "no se encontro $IBC_DIR/config.ini"
fi

section "Permisos de scripts de IBC"
stat -c '%A %n' "$IBC_DIR"/*.sh "$IBC_DIR"/scripts/*.sh 2>/dev/null || echo "no se pudieron leer los permisos"

section "Ultimo log de diagnostico de IBC"
LATEST_LOG=$(find "$TWS_USER_HOME/ibc/logs" -name 'ibc-*.txt' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-)
if [[ -n "$LATEST_LOG" ]]; then
  echo "Archivo: $LATEST_LOG"
  tail -n 100 "$LATEST_LOG"
else
  echo "no se encontro ningun log de IBC en $TWS_USER_HOME/ibc/logs"
fi

section "Linea de lanzamiento de java en ibcstart.sh"
# shellcheck disable=SC2016 # patron literal de busqueda, no se debe expandir
grep -n 'cp "\|launchProgram\|launchJava\|"\$java_path"' "$IBC_DIR/scripts/ibcstart.sh" 2>/dev/null || echo "no se encontro ibcstart.sh"
