#!/usr/bin/env bash
set -euo pipefail

# Simple launcher for the Mimir display client.
# Usage:
#   bash scripts/run.sh [--backend inky|hyperpixelsq|auto] [--debug]
# Backend resolution order:
#   1. --backend argument
#   2. DISPLAY_BACKEND in .env or environment
#   3. auto (let Python loader decide)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}"/.. && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
ENV_FILE="${PROJECT_ROOT}/.env"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[error] .venv not found at $VENV_DIR. Run the installer first." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"

if [[ -f "$ENV_FILE" ]]; then
  # Export variables from .env (simple KEY=VALUE lines, ignore comments)
  while IFS='=' read -r k v; do
    [[ -z "$k" || $k == \#* ]] && continue
    export "$k"="${v}"
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")
fi

REQ_BACKEND=""
DEBUG_FLAG=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --backend)
      REQ_BACKEND=$2; shift 2 ;;
    --debug)
      DEBUG_FLAG="--log-level DEBUG"; shift ;;
    -h|--help)
      echo "Usage: bash scripts/run.sh [--backend inky|hyperpixelsq|auto] [--debug]"; exit 0 ;;
    *)
      echo "[warn] Unknown argument: $1" >&2; shift ;;
  esac
done

BACKEND_TO_USE="${REQ_BACKEND:-${DISPLAY_BACKEND:-auto}}"

echo "[info] Project root: $PROJECT_ROOT"
 echo "[info] Using backend: $BACKEND_TO_USE"

exec python -m mimir_display --backend "$BACKEND_TO_USE" $DEBUG_FLAG
