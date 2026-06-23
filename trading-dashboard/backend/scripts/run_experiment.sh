#!/usr/bin/env bash
# Corre un backtest "antes" (config base) vs "despues" (config con cambios)
# contra el MISMO codigo (mismo commit), y compara los resultados.
#
# A diferencia de probar un cambio de codigo (que requiere dos worktrees en
# distintos commits, ver compare_backtest_snapshots.py), esto sirve para
# experimentos que son puramente de configuracion (pesos de score, filtros,
# etc.): un solo checkout, dos screener.yaml distintos.
#
# Pensado para correrse en background con nohup, ya que cada backtest
# completo (con su walk-forward) tarda varios minutos:
#
#   cd /opt/panel/trading-dashboard/backend
#   nohup bash scripts/run_experiment.sh p1 scripts/experiments/p1_momentum_sin_1m_oportunista_swap.yaml \
#       > /tmp/experiment_p1.log 2>&1 &
#   disown
#
# Para ver el resultado mas tarde (sobrevive un corte de la conexion SSH):
#   cat /tmp/experiment_p1.log
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$#" -lt 2 ]; then
    echo "Uso: run_experiment.sh <etiqueta_despues> <ruta_screener_despues.yaml> [etiqueta_antes] [ruta_screener_antes.yaml]"
    exit 1
fi

LABEL_AFTER="$1"
YAML_AFTER="$2"
LABEL_BEFORE="${3:-baseline}"
YAML_BEFORE="${4:-screener.yaml}"

echo "=== ANTES: corriendo backtest ($LABEL_BEFORE, $YAML_BEFORE) ==="
python scripts/backtest_snapshot.py "$LABEL_BEFORE" "$YAML_BEFORE"

echo
echo "=== DESPUES: corriendo backtest ($LABEL_AFTER, $YAML_AFTER) ==="
python scripts/backtest_snapshot.py "$LABEL_AFTER" "$YAML_AFTER"

echo
echo "=== COMPARACION ==="
python scripts/compare_backtest_snapshots.py "/tmp/backtest_${LABEL_BEFORE}.json" "/tmp/backtest_${LABEL_AFTER}.json"
