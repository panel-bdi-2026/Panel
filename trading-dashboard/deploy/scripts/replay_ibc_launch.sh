#!/usr/bin/env bash
# Repite a mano, en primer plano y sin systemd/IBC de por medio, el ultimo
# comando java que IBC arranco (lo extrae de su propio log de diagnostico).
# Sirve para ver el stdout/stderr real del proceso cuando IBC termina con un
# error (p.ej. exit code 1100) sin loguear ningun detalle util.
#
# Antes de correrlo: systemctl stop ibgateway (para no competir por el
# mismo Xvfb/recursos con el servicio real).
#
# Correr como root: bash replay_ibc_launch.sh
set -euo pipefail

TWS_USER_HOME="${TWS_USER_HOME:-/home/trading}"
DISPLAY_NUM="${IBC_DISPLAY:-:11}"

# El "|| true" evita que un find/grep sin resultados (exit no-cero) tire
# abajo el script en silencio por set -e + pipefail antes de llegar a los
# chequeos de abajo, que son los que de verdad explican el problema.
LOG_FILE=$(find "$TWS_USER_HOME/ibc/logs" -name 'ibc-*.txt' -printf '%T@ %p\n' 2>/dev/null | sort -n | tail -1 | cut -d' ' -f2-) || true
if [[ -z "$LOG_FILE" ]]; then
  echo "No se encontro ningun log de IBC en $TWS_USER_HOME/ibc/logs" >&2
  exit 1
fi

# "ibcsessionid=" solo aparece en la linea real de lanzamiento (con el
# classpath completo), no en la entrada corta "sun.java.command = ..." del
# volcado de System Properties, que tambien menciona IbcGateway.
CMD=$(tac "$LOG_FILE" | grep -m1 -i 'ibcsessionid=') || true
if [[ -z "$CMD" ]]; then
  echo "No se encontro la linea de lanzamiento de java en $LOG_FILE" >&2
  exit 1
fi

echo "Usando log: $LOG_FILE"
echo "Comando encontrado:"
echo "$CMD"
echo

{
  echo '#!/usr/bin/env bash'
  echo "export DISPLAY=$DISPLAY_NUM"
  echo "exec $CMD"
} > /tmp/_ibc_replay_cmd.sh
chmod +x /tmp/_ibc_replay_cmd.sh
chown trading:trading /tmp/_ibc_replay_cmd.sh

Xvfb "$DISPLAY_NUM" -screen 0 1024x768x16 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT
sleep 2

echo "Lanzando ahora en primer plano como usuario trading (Ctrl+C para cortar)..."
# -s /bin/bash evita que su intente usar el shell de login del usuario
# trading (tipicamente nologin en una cuenta de servicio), que es lo que
# causaba "This account is currently not available."
su -s /bin/bash trading -c /tmp/_ibc_replay_cmd.sh
