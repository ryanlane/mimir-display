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
# Usage:
#   ./install_display.sh [--backend auto|inky|hdmi|rgbmatrix|hyperpixelsq]
#                        [--extra all|inky|hdmi|rgbmatrix|hyperpixelsq]
#                        [--mode editable|copy]
#                        [--install-dir /opt/mimir-display]
#                        [--venv .venv] [--service] [--yes]

# Selected extras group for pip install -e ".[EXTRA_NAME]". If unset, derived from backend.
EXTRA_NAME="${DISPLAY_BACKEND:-all}"
VENV_DIR=".venv"
BACKEND=""
MODE="editable"        # editable | copy
INSTALL_DIR=""
AUTO_YES=0              # non-interactive mode
MAKE_SERVICE=0

while [[ "${1:-}" != "" ]]; do
  case "$1" in
    --backend)
      shift
      BACKEND="${1:-}"
      ;;
    --extra)
      shift
      EXTRA_NAME="${1:-all}"
      ;;
    --mode)
      shift
      MODE="${1:-editable}"
      ;;
    --install-dir)
      shift
      INSTALL_DIR="${1:-}"
      ;;
    --venv)
      shift
      VENV_DIR="${1:-.venv}"
      ;;
    --service)
      MAKE_SERVICE=1
      ;;
    -y|--yes)
      AUTO_YES=1
      ;;
    *)
      echo "Unknown arg: $1"
      echo "Usage: $0 [--backend auto|inky|hdmi|rgbmatrix|hyperpixelsq] [--extra group] [--mode editable|copy] [--install-dir PATH] [--venv .venv] [--service] [--yes]"
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
step() { echo; printf "==> %s\n" "$*"; }

in_git_root() {
  # Change to the repository root. Prefer the parent of this script's folder (../)
  # and fall back to git root or walking up until a Python project file is found.
  local script_dir
  script_dir="$(cd -- "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd -P)"

  # First try: parent of the scripts directory (expected project root)
  local parent_dir
  parent_dir="$(dirname "${script_dir}")"
  if [[ -f "${parent_dir}/pyproject.toml" || -f "${parent_dir}/setup.py" ]]; then
    cd -- "${parent_dir}"
    return
  fi

  # Second try: git repository root if available
  if have_cmd git; then
    local git_root
    git_root="$(git -C "${script_dir}" rev-parse --show-toplevel 2>/dev/null || true)"
    if [[ -n "${git_root}" ]]; then
      cd -- "${git_root}"
      return
    fi
  fi

  # Last resort: walk up a few levels looking for a Python project file
  local dir="${script_dir}"
  for _ in 1 2 3 4; do
    if [[ -f "${dir}/pyproject.toml" || -f "${dir}/setup.py" ]]; then
      cd -- "${dir}"
      return
    fi
    dir="$(dirname "${dir}")"
  done

  # Fallback: stay where the script lives
  cd -- "${script_dir}"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

require_cmd() { command -v "$1" >/dev/null 2>&1 || { err "Required command '$1' not found."; exit 1; }; }

apt_try_install() {
  # Best-effort apt install (only if apt-get exists)
  if have_cmd apt-get; then
    sudo apt-get update
    sudo apt-get install -y "$@"
  else
    warn "apt-get not found; skipping install of: $*"
  fi
}

# -----------------------------
# Backend detection and helpers
# -----------------------------
detect_backend() {
  # Lightweight heuristic similar to the Python loader
  if [[ ${FORCE_INKY:-} == 1 ]]; then echo "inky"; return; fi
  if [[ ${FORCE_HDMI:-} == 1 ]]; then echo "hdmi"; return; fi
  if [[ ${FORCE_RGBMATRIX:-} == 1 ]]; then echo "rgbmatrix"; return; fi
  if [[ -e /dev/fb0 ]]; then
    local virt bpp
    virt="$(grep -s '' /sys/class/graphics/fb0/virtual_size || true)"
    bpp="$(grep -s '' /sys/class/graphics/fb0/bits_per_pixel || true)"
    if [[ "$virt" == "720,720" && ( -z "$bpp" || "$bpp" == 16 ) ]]; then
      echo "hyperpixelsq"; return
    fi
    echo "hdmi"; return
  fi
  if have_cmd python3 && python3 - <<'PY' 2>/dev/null; then
import importlib.util
raise SystemExit(0 if importlib.util.find_spec('rgbmatrix') else 1)
PY
    then
      echo "rgbmatrix"; return
  fi
  echo "inky"
}

# Helper: read a key from an env file
read_env_value() {
  local file="$1" key="$2"
  if [[ -f "$file" ]]; then
    awk -F'=' -v k="$key" 'BEGIN{IGNORECASE=0} $0 !~ /^\s*#/ && $1==k {sub(/^\s+|\s+$/, "", $2); print $2; exit}' "$file"
  fi
}

# Helper: set or append KEY=VALUE in an env file
set_env_value() {
  local file="$1" key="$2" value="$3"
  if [[ ! -f "$file" ]]; then
    printf '%s=%s\n' "$key" "$value" >"$file"
    return
  fi
  if grep -q "^${key}=" "$file"; then
    sed -i "s#^${key}=.*#${key}=${value}#" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

# Copy best sample env for backend
copy_sample_env() {
  local backend="$1" dest="$2" root_dir="$3"
  local sample=".env.example"
  case "$backend" in
    hyperpixelsq) sample=".env.example.hyperpixelsq";;
    rgbmatrix)    sample=".env.example.rgbmatrix";;
    *)            sample=".env.example";;
  esac
  if [[ -f "$root_dir/$sample" ]]; then
    cp "$root_dir/$sample" "$dest"
    log "Seeded $dest from $sample"
  else
    : >"$dest"
    warn "Sample $sample not found; created empty $dest"
  fi
}

### -----------------------------
### Environment checks
### -----------------------------
in_git_root

TTY=0
if [[ -t 0 ]]; then TTY=1; fi

# Interactive selection of backend and mode (unless provided)
if [[ -z "$BACKEND" ]]; then
  SUGGESTED="$(detect_backend)"
  if (( AUTO_YES == 1 || TTY == 0 )); then
    BACKEND="$SUGGESTED"
  else
    echo "Available display backends:"
    OPTIONS=(); LABELS=()
    OPTIONS+=("$SUGGESTED"); LABELS+=("$SUGGESTED (detected)")
    for opt in hyperpixelsq hdmi rgbmatrix inky auto; do
      if [[ "$opt" != "$SUGGESTED" ]]; then OPTIONS+=("$opt"); LABELS+=("$opt"); fi
    done
    for i in "${!LABELS[@]}"; do printf "  %d) %s\n" "$((i+1))" "${LABELS[$i]}"; done
    read -rp "Select display backend [1]: " CHOICE
    CHOICE=${CHOICE:-1}
    if ! [[ "$CHOICE" =~ ^[0-9]+$ ]] || (( CHOICE < 1 || CHOICE > ${#OPTIONS[@]} )); then
      warn "Invalid choice; defaulting to option 1 (${OPTIONS[0]})"; CHOICE=1
    fi
    BACKEND="${OPTIONS[$((CHOICE-1))]}"
  fi
fi
log "Selected backend: ${BACKEND:-auto}"

# Derive extras group if not explicitly provided
if [[ -z "$EXTRA_NAME" || "$EXTRA_NAME" == "auto" ]]; then
  case "$BACKEND" in
    inky)         EXTRA_NAME="inky";;
    hyperpixelsq) EXTRA_NAME="hyperpixelsq";;
    rgbmatrix)    EXTRA_NAME="rgbmatrix";;
    hdmi)         EXTRA_NAME="hdmi";;
    auto|*)       EXTRA_NAME="all";;
  esac
fi

# Install mode and directory
if [[ -z "$INSTALL_DIR" ]]; then
  if [[ "$MODE" == "copy" ]]; then
    if (( AUTO_YES == 1 || TTY == 0 )); then
      INSTALL_DIR="/opt/mimir-display"
    else
      read -rp "Install path (directory) [/opt/mimir-display]: " INSTALL_DIR
      INSTALL_DIR=${INSTALL_DIR:-/opt/mimir-display}
    fi
  else
    INSTALL_DIR="$(pwd)"  # in-place editable
  fi
fi

if [[ "$MODE" == "copy" ]]; then
  step "Preparing copy/deploy install to $INSTALL_DIR"
  require_cmd rsync
  if [[ ! -d "$INSTALL_DIR" ]]; then
    if ! mkdir -p "$INSTALL_DIR" 2>/dev/null; then
      log "Creating $INSTALL_DIR with sudo"; sudo mkdir -p "$INSTALL_DIR"
      if [[ -n ${SUDO_USER:-} ]]; then sudo chown -R "$SUDO_USER":"$SUDO_USER" "$INSTALL_DIR" || true; else sudo chown -R "$USER":"$USER" "$INSTALL_DIR" || true; fi
    fi
  fi
  RSYNC_CMD=(rsync -a --delete --exclude '.venv' --exclude '.git')
  if [[ -w "$INSTALL_DIR" ]]; then
    "${RSYNC_CMD[@]}" "$(pwd)/" "$INSTALL_DIR/"
  else
    sudo "${RSYNC_CMD[@]}" "$(pwd)/" "$INSTALL_DIR/"
    if [[ -n ${SUDO_USER:-} ]]; then sudo chown -R "$SUDO_USER":"$SUDO_USER" "$INSTALL_DIR" || true; else sudo chown -R "$USER":"$USER" "$INSTALL_DIR" || true; fi
  fi
  cd -- "$INSTALL_DIR"
else
  step "Editable install in current project root"
fi

# Verify we are at a Python project root
if [[ ! -f "pyproject.toml" && ! -f "setup.py" ]]; then
  err "Neither pyproject.toml nor setup.py found in $(pwd). Ensure you run from a valid project root."
  exit 1
fi

log "Project root: $(pwd)"

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
    if (( AUTO_YES == 1 )); then
      warn "Creating venv with --system-site-packages so system NumPy can be used."
      VENV_FLAGS+=(--system-site-packages)
    else
      read -rp "Use --system-site-packages venv to leverage system NumPy? (Y/n): " SYSNP
      SYSNP=${SYSNP:-Y}
      if [[ ${SYSNP,,} != n* ]]; then VENV_FLAGS+=(--system-site-packages); fi
    fi
  fi
  log "Creating virtualenv at ${VENV_DIR} ..."
  # Options must come before the target directory
  "$PY" -m venv ${VENV_FLAGS:+"${VENV_FLAGS[@]}"} "${VENV_DIR}"
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
if [[ "$MODE" == "copy" ]]; then
  step "Installing mimir-display (standard mode) with extras: [${EXTRA_NAME}]"
  python -m pip install ".[${EXTRA_NAME}]" -v
else
  step "Installing mimir-display (editable mode) with extras: [${EXTRA_NAME}]"
  python -m pip install -e ".[${EXTRA_NAME}]" -v
fi

### -----------------------------
### Environment configuration (.env)
### -----------------------------
step "Configuring environment (.env)"
ENV_FILE=".env"
if [[ -f "$ENV_FILE" ]]; then
  log "Existing .env found; updating key settings"
else
  log "Seeding .env for backend: $BACKEND"
  copy_sample_env "$BACKEND" "$ENV_FILE" "$(pwd)"
fi

set_env_value "$ENV_FILE" "DISPLAY_BACKEND" "${BACKEND:-auto}"
if ! grep -q '^LOG_LEVEL=' "$ENV_FILE"; then echo "LOG_LEVEL=INFO" >>"$ENV_FILE"; fi

cur_platform_url="$(read_env_value "$ENV_FILE" "PLATFORM_URL")"; cur_platform_url=${cur_platform_url:-http://localhost:5000}
cur_mqtt_host="$(read_env_value "$ENV_FILE" "MQTT_BROKER_HOST")"; cur_mqtt_host=${cur_mqtt_host:-localhost}
orientation_default="landscape"

if (( AUTO_YES == 1 || TTY == 0 )); then
  set_env_value "$ENV_FILE" "PLATFORM_URL" "$cur_platform_url"
  set_env_value "$ENV_FILE" "MQTT_BROKER_HOST" "$cur_mqtt_host"
  set_env_value "$ENV_FILE" "DISPLAY_ORIENTATION" "$orientation_default"
else
  read -rp "Platform URL [${cur_platform_url}]: " INPUT_PLATFORM_URL; INPUT_PLATFORM_URL=${INPUT_PLATFORM_URL:-$cur_platform_url}
  set_env_value "$ENV_FILE" "PLATFORM_URL" "$INPUT_PLATFORM_URL"
  read -rp "MQTT broker host [${cur_mqtt_host}]: " INPUT_MQTT_HOST; INPUT_MQTT_HOST=${INPUT_MQTT_HOST:-$cur_mqtt_host}
  set_env_value "$ENV_FILE" "MQTT_BROKER_HOST" "$INPUT_MQTT_HOST"
  read -rp "Display orientation (landscape|portrait_left|portrait_right) [landscape]: " ORIENTATION_INPUT; ORIENTATION_INPUT=${ORIENTATION_INPUT:-landscape}
  case "$ORIENTATION_INPUT" in
    landscape|portrait_left|portrait_right) : ;;
    *) warn "Invalid orientation '$ORIENTATION_INPUT'; defaulting to landscape"; ORIENTATION_INPUT=landscape ;;
  esac
  set_env_value "$ENV_FILE" "DISPLAY_ORIENTATION" "$ORIENTATION_INPUT"
fi

if [[ "$BACKEND" == "hyperpixelsq" ]]; then
  BOOT_CANDIDATES=(/boot/firmware/config.txt /boot/config.txt)
  CONFIG_PATH=""
  for p in "${BOOT_CANDIDATES[@]}"; do if [[ -f $p ]]; then CONFIG_PATH=$p; break; fi; done
  if [[ -n $CONFIG_PATH ]]; then
    OVERLAY_LINE="dtoverlay=vc4-kms-dpi-hyperpixel4sq"
    if ! grep -q "^${OVERLAY_LINE}" "$CONFIG_PATH"; then
      if (( AUTO_YES == 1 )); then
        warn "Appending HyperPixel overlay to $CONFIG_PATH (requires reboot)"
        echo "$OVERLAY_LINE" | sudo tee -a "$CONFIG_PATH" >/dev/null
      else
        read -rp "Append '${OVERLAY_LINE}' to $CONFIG_PATH now? (y/N): " ADD_OVL
        if [[ ${ADD_OVL,,} == y* ]]; then echo "$OVERLAY_LINE" | sudo tee -a "$CONFIG_PATH" >/dev/null; fi
      fi
    fi
  else
    warn "Could not locate config.txt to verify HyperPixel overlay."
  fi
fi

### -----------------------------
### Optional systemd service
### -----------------------------
if (( MAKE_SERVICE == 1 )) || { (( AUTO_YES == 0 )) && (( TTY == 1 )) && read -rp "Create systemd service? (y/N): " MAKE_SVC_ANS && [[ ${MAKE_SVC_ANS,,} == y* ]]; }; then
  step "Creating systemd service"
  SERVICE_PATH="/etc/systemd/system/mimir-display.service"
  log "Writing $SERVICE_PATH"
  cat <<SERVICE | sudo tee "$SERVICE_PATH" >/dev/null
[Unit]
Description=Mimir Unified Display Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
EnvironmentFile=$(pwd)/.env
ExecStart=$(pwd)/${VENV_DIR}/bin/python -m mimir_display --backend ${BACKEND}
Restart=on-failure
RestartSec=3
User=${SUDO_USER:-$USER}
Group=video

[Install]
WantedBy=multi-user.target
SERVICE
  sudo systemctl daemon-reload
  sudo systemctl enable mimir-display.service
  log "Service installed. Start with: sudo systemctl start mimir-display"
fi

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
echo "Run client:      mimir-display --backend ${BACKEND:-auto}"
echo "Service:         sudo systemctl start mimir-display (if created)"
