#!/usr/bin/env bash
# Hace TODO el ciclo de los experimentos de backtest con un solo comando,
# corrido desde TU MAQUINA (no desde el droplet, igual que oci_launch_retry.sh
# en este mismo directorio): necesita ssh/scp locales con acceso por Tailscale
# a 100.92.236.44, el mismo acceso que ya usas para conectarte a mano.
#
# Que hace, sin que tengas que copiar/pegar nada mas mientras corre:
#   1. Por SSH: git pull en /opt/panel (trae este script y los experimentos
#      mas nuevos que ya esten commiteados en la rama de trabajo).
#   2. Lanza scripts/run_all_experiments.sh en el droplet con nohup+disown
#      (sobrevive si esta conexion SSH se corta, como ya nos paso antes).
#   3. Espera solo, sondeando cada POLL_SECONDS si el batch termino.
#   4. Cuando termina, trae el resumen consolidado por scp y lo imprime y
#      guarda localmente.
#
# Si la conexion se corta mientras espera (paso 3), podes volver a correr
# este mismo script: detecta por PID si ya hay un batch corriendo en el
# droplet y, en ese caso, NO lanza uno nuevo (evitaria dos corridas pisandose
# los mismos archivos /tmp/backtest_*.json) -- simplemente se une a esperar
# el que ya esta corriendo.
#
# Uso (desde tu maquina):
#   bash trading-dashboard/deploy/scripts/deploy_and_run_experiments.sh
set -uo pipefail

HOST="${HOST:-root@100.92.236.44}"
BRANCH="${BRANCH:-claude/investment-dashboard-trades-x2vkn8}"
REMOTE_REPO="${REMOTE_REPO:-/opt/panel}"
REMOTE_BACKEND="$REMOTE_REPO/trading-dashboard/backend"
REMOTE_DONE_MARKER="/tmp/experiments.done"
REMOTE_PID_FILE="/tmp/experiments.pid"
REMOTE_SUMMARY="/tmp/experiments_summary.txt"
LOCAL_OUT="experiments_summary_$(date +%Y%m%d_%H%M%S).txt"
POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-7200}"  # 2 horas, por si algo se cuelga

ssh_opts=(-o ConnectTimeout=10)

echo "== 1/4 Actualizando codigo en el droplet =="
ssh "${ssh_opts[@]}" "$HOST" "cd '$REMOTE_REPO' && git fetch origin && git pull origin '$BRANCH'" || {
    echo "No se pudo hacer git pull en el droplet. Revisa la conexion (Tailscale) y reintenta." >&2
    exit 1
}

echo
echo "== 2/4 Lanzando el batch de experimentos en background =="
launch_status="$(ssh "${ssh_opts[@]}" "$HOST" "
    cd '$REMOTE_BACKEND'
    if [ -f '$REMOTE_PID_FILE' ] && kill -0 \"\$(cat '$REMOTE_PID_FILE' 2>/dev/null)\" 2>/dev/null; then
        echo YA_CORRIENDO
    else
        rm -f '$REMOTE_DONE_MARKER'
        nohup bash -c 'bash scripts/run_all_experiments.sh; touch $REMOTE_DONE_MARKER; rm -f $REMOTE_PID_FILE' > /tmp/experiments.log 2>&1 &
        echo \$! > '$REMOTE_PID_FILE'
        disown
        echo LANZADO
    fi
")" || {
    echo "No se pudo lanzar/verificar el batch en el droplet." >&2
    exit 1
}
if [ "$launch_status" = "YA_CORRIENDO" ]; then
    echo "Ya habia un batch corriendo desde antes -- me uno a esperar que termine (no lance uno nuevo)."
else
    echo "Batch lanzado."
fi

echo
echo "== 3/4 Esperando a que termine (puede tardar bastante; no hace falta que hagas nada) =="
elapsed=0
while true; do
    if ssh "${ssh_opts[@]}" "$HOST" "test -f '$REMOTE_DONE_MARKER'" 2>/dev/null; then
        echo "Listo, el batch termino."
        break
    fi
    if [ "$elapsed" -ge "$MAX_WAIT_SECONDS" ]; then
        echo "Pasaron $MAX_WAIT_SECONDS segundos sin terminar. Reviso manualmente con:"
        echo "  ssh $HOST 'tail -f /tmp/experiments.log'"
        exit 1
    fi
    sleep "$POLL_SECONDS"
    elapsed=$((elapsed + POLL_SECONDS))
    echo "  ... todavia corriendo (${elapsed}s), sigo esperando."
done

echo
echo "== 4/4 Trayendo el resultado consolidado =="
scp "${ssh_opts[@]}" "$HOST:$REMOTE_SUMMARY" "$LOCAL_OUT" || {
    echo "No se pudo traer $REMOTE_SUMMARY. Podes verlo directo con:"
    echo "  ssh $HOST 'cat $REMOTE_SUMMARY'"
    exit 1
}

echo
cat "$LOCAL_OUT"
echo
echo "(guardado tambien en ./$LOCAL_OUT)"
