#!/usr/bin/env bash
# Reintenta crear una instancia ARM Ampere (VM.Standard.A1.Flex) en Oracle
# Cloud hasta que haya capacidad disponible, con backoff para no chocar contra
# el limite de rate ("Too many requests for the user"). Pensado para el tier
# Always Free, donde el error "Out of capacity for shape VM.Standard.A1.Flex"
# es habitual y la capacidad se libera y se ocupa todo el tiempo.
#
# Requisitos:
#   - OCI CLI instalada y configurada en ESTA maquina (no en la VM):
#       https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm
#       oci setup config   # genera ~/.oci/config con tu API key
#   - Los OCID de tu tenancy (ver oci_launch.env.example para como obtenerlos).
#   - jq es opcional: si esta, al final muestra la IP publica de la VM.
#
# Uso:
#   cp oci_launch.env.example oci_launch.env
#   # editar oci_launch.env con tus valores
#   bash oci_launch_retry.sh            # busca oci_launch.env al lado del script
#   bash oci_launch_retry.sh otra.env   # o pasale otro archivo de config
#
# El script NO contiene credenciales: la autenticacion sale de ~/.oci/config y
# los OCID/clave SSH se cargan desde el archivo .env que vos completas y que NO
# se versiona (esta en .gitignore).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${1:-$SCRIPT_DIR/oci_launch.env}"

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: no encuentro el archivo de config '$ENV_FILE'." >&2
  echo "Copia oci_launch.env.example a oci_launch.env y completalo." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

# --- Validacion de los valores obligatorios ---------------------------------
: "${OCI_COMPARTMENT_ID:?Falta OCI_COMPARTMENT_ID en el .env}"
: "${OCI_AVAILABILITY_DOMAIN:?Falta OCI_AVAILABILITY_DOMAIN en el .env}"
: "${OCI_SUBNET_ID:?Falta OCI_SUBNET_ID en el .env}"
: "${OCI_IMAGE_ID:?Falta OCI_IMAGE_ID en el .env}"
: "${OCI_SSH_PUBKEY_FILE:?Falta OCI_SSH_PUBKEY_FILE en el .env}"

OCI_SHAPE="${OCI_SHAPE:-VM.Standard.A1.Flex}"
OCI_OCPUS="${OCI_OCPUS:-1}"
OCI_MEMORY_GB="${OCI_MEMORY_GB:-6}"
OCI_DISPLAY_NAME="${OCI_DISPLAY_NAME:-panel-trading}"
OCI_ASSIGN_PUBLIC_IP="${OCI_ASSIGN_PUBLIC_IP:-true}"
RETRY_DELAY="${RETRY_DELAY:-60}"      # segundos base entre reintentos por capacidad
RATE_DELAY="${RATE_DELAY:-120}"       # segundos iniciales tras un "Too many requests"
MAX_RATE_DELAY="${MAX_RATE_DELAY:-900}"  # tope del backoff de rate (15 min)
MAX_ATTEMPTS="${MAX_ATTEMPTS:-0}"     # 0 = reintentar para siempre

if [ ! -f "$OCI_SSH_PUBKEY_FILE" ]; then
  echo "ERROR: no encuentro la clave SSH publica '$OCI_SSH_PUBKEY_FILE'." >&2
  exit 1
fi

if ! command -v oci >/dev/null 2>&1; then
  echo "ERROR: la OCI CLI ('oci') no esta instalada o no esta en el PATH." >&2
  exit 1
fi

# La metadata (clave SSH) se pasa via archivo JSON temporal: una clave publica
# es una sola linea sin comillas, asi que el JSON resultante es valido.
META_FILE="$(mktemp)"
trap 'rm -f "$META_FILE"' EXIT
printf '{"ssh_authorized_keys": "%s"}' "$(cat "$OCI_SSH_PUBKEY_FILE")" > "$META_FILE"

echo "Lanzando $OCI_SHAPE ($OCI_OCPUS OCPU / ${OCI_MEMORY_GB} GB) en $OCI_AVAILABILITY_DOMAIN"
echo "Reintentos: por capacidad cada ${RETRY_DELAY}s; backoff de rate desde ${RATE_DELAY}s."
echo "Cancela con Ctrl-C cuando quieras. No crea duplicados: para en cuanto una entra."
echo

attempt=0
rate_delay="$RATE_DELAY"
while true; do
  attempt=$((attempt + 1))
  printf '[%s] Intento #%d ... ' "$(date '+%H:%M:%S')" "$attempt"

  set +e
  out="$(oci compute instance launch \
    --compartment-id "$OCI_COMPARTMENT_ID" \
    --availability-domain "$OCI_AVAILABILITY_DOMAIN" \
    --subnet-id "$OCI_SUBNET_ID" \
    --image-id "$OCI_IMAGE_ID" \
    --shape "$OCI_SHAPE" \
    --shape-config "{\"ocpus\": $OCI_OCPUS, \"memoryInGBs\": $OCI_MEMORY_GB}" \
    --display-name "$OCI_DISPLAY_NAME" \
    --assign-public-ip "$OCI_ASSIGN_PUBLIC_IP" \
    --metadata "file://$META_FILE" \
    --wait-for-state RUNNING \
    2>&1)"
  code=$?
  set -e

  if [ $code -eq 0 ]; then
    echo "OK"
    echo
    echo "Instancia creada y en estado RUNNING."
    if command -v jq >/dev/null 2>&1; then
      instance_id="$(echo "$out" | jq -r '.data.id // empty')"
      if [ -n "$instance_id" ]; then
        echo "OCID: $instance_id"
        public_ip="$(oci compute instance list-vnics --instance-id "$instance_id" \
          --query 'data[0]."public-ip"' --raw-output 2>/dev/null || true)"
        [ -n "$public_ip" ] && [ "$public_ip" != "null" ] && \
          echo "IP publica: $public_ip" && \
          echo "Conectate con:  ssh ubuntu@$public_ip"
      fi
    else
      echo "(instala 'jq' si queres que el script imprima la IP publica automaticamente;"
      echo " si no, miratela en la consola: Compute -> Instances -> tu instancia)"
    fi
    exit 0
  fi

  # Sin capacidad ARM: el caso esperado. Reintento tras una pausa fija.
  if echo "$out" | grep -qiE "Out of capacity|Out of host capacity|InternalError|status: 500"; then
    echo "sin capacidad, reintento en ${RETRY_DELAY}s"
    rate_delay="$RATE_DELAY"  # reseteo el backoff de rate: este error no es por rate
    sleep "$RETRY_DELAY"
  # Rate limit: espacio mas los reintentos con backoff exponencial.
  elif echo "$out" | grep -qiE "TooManyRequests|Too many requests|status: 429"; then
    echo "rate limit, espero ${rate_delay}s (backoff)"
    sleep "$rate_delay"
    rate_delay=$(( rate_delay * 2 ))
    [ "$rate_delay" -gt "$MAX_RATE_DELAY" ] && rate_delay="$MAX_RATE_DELAY"
  else
    # Cualquier otro error (auth, OCID mal, LimitExceeded, etc.) NO se reintenta:
    # reintentar no lo va a arreglar y solo gastaria requests.
    echo "ERROR no recuperable:"
    echo "$out" >&2
    exit 1
  fi

  if [ "$MAX_ATTEMPTS" -gt 0 ] && [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "Alcanzado MAX_ATTEMPTS=$MAX_ATTEMPTS sin exito. Pruebo de nuevo mas tarde."
    exit 2
  fi
done
