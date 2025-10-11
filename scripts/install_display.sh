#!/usr/bin/env bash
# install_display.sh
# Robust installer for mimir-display on Raspberry Pi.
# - Prefers prebuilt ARM wheels via PiWheels
# - Preinstalls NumPy as a wheel (fail-fast if none exists)
# - Falls back sensibly on ARMv6 (Pi Zero/Zero W)

set -euo pipefail

### -----------------------------
### Configuration / arguments
### -----------------------------
# Usage: ./install_display.sh [--extra backends] [--venv .venv]
EXTRA="${DISPLAY_BACKEND:-all}"
VENV_DIR=".venv"

while [[ "${1:-}" != "" ]]; do
  case "$1" in
    --extra)
      shift
      EXTRA="${1:-all}"
      ;;
    --venv)
      shift
      VENV_DIR="${1:-.venv}"
      ;;
    *)
      echo "Unknown arg: $1"
      echo "Usage: $0 [--extra backends] [--venv .venv]"
      exit 2
      ;;
  esac
  shift || true
done

### -----------------------------
### Helpers
### -----------------------------
log()  { printf "\033[1;34m[info]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[warn]\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m[err]\033[0m  %s\n" "$*"; }

in_git_root() {
  # cd to the directory containing this script (project root assumed)
  cd -- "$(dirname "$0")"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

apt_try_install() {
  # Best-effort apt install (only if apt-get exists)
  if have_cmd apt-get; then
    sudo apt-get update
    sudo apt-get install -y "$@"
  else
    warn "apt-get not found; skipping install of: $*"
  fi
}

### -----------------------------
### Environment checks
### -----------------------------
in_git_root

ARCH="$(uname -m || true)"
PY="$(command -v python3 || true)"
if [[ -z "${PY}" ]]; then
  err "python3 not found. Please install Python 3."
  exit 1
fi

PY_VER="$("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
log "Python: ${PY} (${PY_VER})"
log "Arch:   ${ARCH}"

### -----------------------------
### Prefer PiWheels (wheels for ARM)
### -----------------------------
# Make PiWheels the primary index, keep PyPI as fallback for anything not on PiWheels.
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://www.piwheels.org/simple}"
export PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-https://pypi.org/simple}"

log "Using PIP_INDEX_URL=${PIP_INDEX_URL}"
log "Using PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL}"

### -----------------------------
### Create virtualenv
### -----------------------------
if [[ -d "${VENV_DIR}" ]]; then
  log "Virtualenv ${VENV_DIR} already exists; reusing."
else
  # On ARMv6 (Pi Zero/Zero W), allow system-site-packages so we can use system numpy if needed.
  VENV_FLAGS=()
  if [[ "${ARCH}" == "armv6l" ]]; then
    warn "ARMv6 detected (Pi Zero/Zero W). NumPy wheels will likely be unavailable."
    warn "Creating venv with --system-site-packages so system NumPy can be used."
    VENV_FLAGS+=(--system-site-packages)
  fi
  log "Creating virtualenv at ${VENV_DIR} ..."
  "$PY" -m venv "${VENV_DIR}" "${VENV_FLAGS[@]}"
fi

# shellcheck disable=SC1090
source "${VENV_DIR}/bin/activate"

### -----------------------------
### Upgrade core build tools
### -----------------------------
log "Upgrading pip/setuptools/wheel ..."
python -m pip install -U pip setuptools wheel

### -----------------------------
### ARMv6 special handling (system numpy)
### -----------------------------
if [[ "${ARCH}" == "armv6l" ]]; then
  log "Checking for system NumPy (ARMv6)..."
  if ! python - <<'PY'
try:
    import numpy
    print("Found system NumPy:", numpy.__version__)
except Exception:
    raise SystemExit(1)
PY
  then
    warn "System NumPy not found. Attempting to install python3-numpy via apt..."
    apt_try_install python3-numpy || true
    if ! python - <<'PY'
try:
    import numpy
    print("Found system NumPy after apt:", numpy.__version__)
except Exception:
    raise SystemExit(1)
PY
    then
      warn "Still no NumPy available on ARMv6. Many packages will try to build from source and hang."
      warn "Consider moving heavy NumPy work off-device or using a Pi 3/4/5 (armv7l/aarch64)."
    fi
  fi
fi

### -----------------------------
### Preinstall NumPy as a wheel (if possible)
### -----------------------------
log "Attempting to preinstall NumPy as a wheel (to avoid source builds) ..."
# We pin <2 to match your mimir-display==1.0.3 requirement; adjust if you relax that pin.
if ! python -m pip install --only-binary=:all: "numpy<2" ; then
  warn "No suitable NumPy wheel for this Python/arch. We'll proceed, but pip may attempt a slow source build."
  warn "If install stalls at 'Installing backend dependencies ...', relax the NumPy pin or switch to aarch64 with PiWheels."
fi

### -----------------------------
### Install project (editable) with selected extras
### -----------------------------
# EXTRA can be: "all", "hdmi", "inky", etc. (matches your setup.cfg/pyproject extras)
# Examples:
#   --extra all
#   --extra hdmi
#   --extra inky
log "Installing mimir-display in editable mode with extras: [${EXTRA}] ..."
# More verbosity helps when debugging wheel vs source build behavior.
python -m pip install -e ".[${EXTRA}]" -v

### -----------------------------
### Post-install sanity check
### -----------------------------
log "Verifying NumPy import (optional)..."
python - <<'PY' || true
try:
    import numpy as np
    import platform
    print("NumPy OK:", np.__version__, "on", platform.machine())
except Exception as e:
    print("NumPy not available:", e)
PY

log "Install complete ✅"
echo
echo "To activate the environment later:"
echo "  source ${VENV_DIR}/bin/activate"
echo
