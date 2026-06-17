#!/usr/bin/env bash
# Arranca un Xvfb (pantalla virtual) y despues IB Gateway via IBC. IB Gateway
# es una aplicacion Java con interfaz grafica: Xvfb le da una "pantalla" sin
# necesitar monitor ni VNC. Pensado para correr como el ExecStart de
# systemd/ibgateway.service.
set -euo pipefail

DISPLAY_NUM="${IBC_DISPLAY:-:10}"
IBC_DIR="${IBC_DIR:-/opt/ibc}"

Xvfb "$DISPLAY_NUM" -screen 0 1024x768x16 &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT

export DISPLAY="$DISPLAY_NUM"
sleep 2  # darle tiempo a Xvfb a levantar antes de lanzar IBC

exec "$IBC_DIR/gatewaystart.sh"
