#!/usr/bin/env bash
# Corre TODOS los escenarios de scripts/experiments/*.yaml contra el mismo
# codigo (mismo commit), cada uno contra la config base (screener.yaml), en
# un solo batch desatendido. A diferencia de run_experiment.sh (que corre UN
# par antes/despues por invocacion), este script no recibe argumentos: arma
# la lista de escenarios solo, recorriendola con un glob, asi que agregar un
# experimento nuevo es tan simple como dejar un .yaml mas en
# scripts/experiments/ y volver a correr este script -- no hay que editarlo.
#
# Pensado para correrse una sola vez con nohup y traerse el resultado
# consolidado mas tarde, en vez de repetir a mano el ciclo
# nohup->tail->comparar por cada escenario:
#
#   cd /opt/panel/trading-dashboard/backend
#   nohup bash scripts/run_all_experiments.sh > /tmp/experiments.log 2>&1 &
#   disown
#
# Para ver el resultado consolidado mas tarde (sobrevive un corte de SSH):
#   cat /tmp/experiments_summary.txt
#
# Un escenario que falle (ej. config invalida) no aborta el resto del batch:
# queda marcado como FALLO en el resumen y el script sigue con el siguiente.
set -uo pipefail
cd "$(dirname "$0")/.."

SUMMARY="/tmp/experiments_summary.txt"
: > "$SUMMARY"

run_step() {
    if ! "$@" 2>&1 | tee -a "$SUMMARY"; then
        echo "  *** FALLO (ver arriba) ***" | tee -a "$SUMMARY"
    fi
}

echo "=== BASELINE: corriendo backtest (screener.yaml) ===" | tee -a "$SUMMARY"
run_step python scripts/backtest_snapshot.py baseline screener.yaml

shopt -s nullglob
experiments=(scripts/experiments/*.yaml)
if [ ${#experiments[@]} -eq 0 ]; then
    echo | tee -a "$SUMMARY"
    echo "No hay archivos en scripts/experiments/*.yaml -- nada mas para correr." | tee -a "$SUMMARY"
    exit 0
fi

for path in "${experiments[@]}"; do
    label="$(basename "$path" .yaml)"
    echo | tee -a "$SUMMARY"
    echo "=== ESCENARIO '$label' ($path): corriendo backtest ===" | tee -a "$SUMMARY"
    run_step python scripts/backtest_snapshot.py "$label" "$path"
done

for path in "${experiments[@]}"; do
    label="$(basename "$path" .yaml)"
    echo | tee -a "$SUMMARY"
    echo "=== COMPARACION: baseline vs '$label' ===" | tee -a "$SUMMARY"
    run_step python scripts/compare_backtest_snapshots.py /tmp/backtest_baseline.json "/tmp/backtest_${label}.json"
done

echo | tee -a "$SUMMARY"
echo "=== LISTO. Resultado consolidado en $SUMMARY ===" | tee -a "$SUMMARY"
