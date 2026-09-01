#!/usr/bin/env bash
# ExecStartPre de trading-dashboard.service: espera a que IB Gateway/TWS
# acepte conexiones TCP en IB_HOST:IB_PORT antes de arrancar el backend.
#
# Sin esto, app.main:lifespan() intenta conectar a IBKR una sola vez al
# arrancar y, si IB Gateway todavia no esta listo (Xvfb + Java + login
# automatizado via IBC puede tardar 30-90+ segundos, mas todavia despues del
# reinicio forzado semanal de IBKR los domingos a la 1am ET), el backend
# queda permanentemente desconectado hasta un restart manual o un POST
# /api/mode -- no hay reintento automatico de la conexion inicial.
#
# El timeout de este script es corto a proposito: si IB Gateway no esta
# listo, este script falla y deja que Restart=always + RestartSec de la
# unidad reintenten el arranque completo de trading-dashboard.service (lo
# que incluye volver a correr este script). Por eso StartLimitIntervalSec/
# StartLimitBurst estan ajustados en trading-dashboard.service: para que
# systemd no agote su budget de reinicios antes de que IB Gateway termine
# de levantar.
set -uo pipefail

IB_HOST="${IB_HOST:-127.0.0.1}"
IB_PORT="${IB_PORT:-7497}"
TIMEOUT_SECONDS="${WAIT_FOR_IB_TIMEOUT_SECONDS:-20}"
POLL_INTERVAL_SECONDS="${WAIT_FOR_IB_POLL_INTERVAL_SECONDS:-2}"

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

while true; do
    if (exec 3<>"/dev/tcp/${IB_HOST}/${IB_PORT}") 2>/dev/null; then
        exec 3>&- 3<&- 2>/dev/null
        echo "wait_for_ibgateway: ${IB_HOST}:${IB_PORT} acepta conexiones."
        exit 0
    fi

    if (( $(date +%s) >= deadline )); then
        echo "wait_for_ibgateway: timeout esperando ${IB_HOST}:${IB_PORT} (${TIMEOUT_SECONDS}s)." >&2
        exit 1
    fi

    sleep "${POLL_INTERVAL_SECONDS}"
done
