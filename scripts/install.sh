#!/usr/bin/env bash
set -euo pipefail

# Mimir Display Client Install Script
# Idempotent-ish installer for Debian/Ubuntu/Raspbian-like systems.
# Usage: sudo ./scripts/install.sh [--wheel /path/to/mimir_display-*.whl] [--user mimir] [--prefix /opt/mimir-display]

WHEEL_PATH=""
SERVICE_USER="mimir"
PREFIX="/opt/mimir-display"
PYTHON_BIN="python3"
VENV_DIR="$PREFIX/venv"
WORK_DIR="/var/lib/mimir-display"
ETC_DIR="/etc/mimir-display"
SERVICE_FILE_SOURCE="packaging/mimir-display.service"
SERVICE_FILE_TARGET="/etc/systemd/system/mimir-display.service"
DEFAULT_STATE_SUBDIR="state"

# Legacy Pi Zero (armv6) handling
LEGACY_MODE=0           # auto-detected or via --legacy-zero
INSTALL_SYSTEM_DEPS=0   # enabled via --install-system-deps
VENV_CREATE_FLAGS=""    # may include --system-site-packages in legacy mode
MARKER_FILE="$WORK_DIR/LEGACY_ZERO"

color() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info() { echo "$(color 36 [INFO]) $*"; }
warn() { echo "$(color 33 [WARN]) $*"; }
err()  { echo "$(color 31 [ERR])  $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wheel) WHEEL_PATH="$2"; shift 2;;
    --user) SERVICE_USER="$2"; shift 2;;
    --prefix) PREFIX="$2"; VENV_DIR="$PREFIX/venv"; shift 2;;
    --python) PYTHON_BIN="$2"; shift 2;;
    --legacy-zero) LEGACY_MODE=1; shift 1;;
    --install-system-deps) INSTALL_SYSTEM_DEPS=1; shift 1;;
    *) err "Unknown arg: $1"; exit 1;;
  esac
done
detect_legacy_arch() {
  # Only auto-detect if not forced via flag
  if [[ $LEGACY_MODE -eq 1 ]]; then return; fi
  local model_file="/proc/device-tree/model"
  if [[ -f $model_file ]] && grep -qi 'raspberry pi zero' "$model_file" && ! grep -qi 'zero 2' "$model_file"; then
    LEGACY_MODE=1
  fi
  local arch
  arch=$(uname -m 2>/dev/null || echo unknown)
  if [[ $arch == "armv6l" ]]; then
    LEGACY_MODE=1
  fi
}

prepare_legacy_mode() {
  if [[ $LEGACY_MODE -eq 1 ]]; then
    VENV_CREATE_FLAGS="--system-site-packages"
    info "Legacy Pi Zero (armv6) mode enabled: using system site packages"
    if [[ $INSTALL_SYSTEM_DEPS -eq 1 ]]; then
      if command -v apt-get >/dev/null 2>&1; then
        info "Installing system dependencies (numpy, openblas)"
        apt-get update -y || warn "apt-get update failed"
        apt-get install -y python3-numpy libopenblas0 || warn "apt-get install partial failure"
      else
        warn "apt-get not available; cannot install system deps"
      fi
    fi
  fi
}

require_root() {
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (use sudo)."; exit 1; fi
}

ensure_user() {
  if id "$SERVICE_USER" >/dev/null 2>&1; then
    info "User $SERVICE_USER exists"; else
    info "Creating system user $SERVICE_USER";
    useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER" || true;
  fi
}

create_dirs() {
  mkdir -p "$PREFIX" "$WORK_DIR" "$ETC_DIR"
  # Determine state dir precedence: existing .env MIMIR_STATE_DIR value -> default subdir
  local state_dir=""
  if [[ -f "$ETC_DIR/.env" ]]; then
    # shellcheck disable=SC2046
    state_dir=$(grep -E '^MIMIR_STATE_DIR=' "$ETC_DIR/.env" | head -n1 | cut -d'=' -f2- || true)
  fi
  if [[ -z "$state_dir" ]]; then
    state_dir="$WORK_DIR/$DEFAULT_STATE_SUBDIR"
  fi
  mkdir -p "$state_dir"
  # Cache directory (optional) precedence: .env MIMIR_CACHE_DIR -> $WORK_DIR/cache
  local cache_dir=""
  if [[ -f "$ETC_DIR/.env" ]]; then
    cache_dir=$(grep -E '^MIMIR_CACHE_DIR=' "$ETC_DIR/.env" | head -n1 | cut -d'=' -f2- || true)
  fi
  if [[ -z "$cache_dir" ]]; then
    cache_dir="$WORK_DIR/cache"
  fi
  mkdir -p "$cache_dir"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$WORK_DIR"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$state_dir"
  chown -R "$SERVICE_USER:$SERVICE_USER" "$cache_dir"
}

create_venv() {
  # Preflight: ensure ensurepip / venv module available
  if ! $PYTHON_BIN -m venv --help >/dev/null 2>&1; then
    err "Python venv module not available. Install python3-venv (apt-get install -y python3-venv) and re-run."; exit 1; fi

  local needs_recreate=0
  if [[ -d "$VENV_DIR" ]]; then
    # Check for broken/partial venv (missing activate or python)
    if [[ ! -f "$VENV_DIR/bin/activate" || ! -x "$VENV_DIR/bin/python" ]]; then
      warn "Existing virtualenv appears incomplete; removing"
      rm -rf "$VENV_DIR"
      needs_recreate=1
    fi
  else
    needs_recreate=1
  fi
  if [[ $needs_recreate -eq 1 ]]; then
    info "Creating virtualenv at $VENV_DIR (flags: $VENV_CREATE_FLAGS)"
    $PYTHON_BIN -m venv $VENV_CREATE_FLAGS "$VENV_DIR"
  else
    info "Using existing virtualenv $VENV_DIR"
  fi
  # shellcheck source=/dev/null
  source "$VENV_DIR/bin/activate"
  pip install --upgrade pip wheel >/dev/null
}

install_package() {
  source "$VENV_DIR/bin/activate"
  if [[ -n "$WHEEL_PATH" ]]; then
    info "Installing wheel $WHEEL_PATH"
    pip install "$WHEEL_PATH"
  else
    info "No wheel specified; attempting editable install from current directory"
    pip install .
  fi
  if [[ $LEGACY_MODE -eq 1 ]]; then
    touch "$MARKER_FILE" || true
    info "Marked legacy installation at $MARKER_FILE"
  fi
}

install_service() {
  if [[ ! -f "$SERVICE_FILE_SOURCE" ]]; then
    err "Service file template missing: $SERVICE_FILE_SOURCE"; exit 1; fi
  cp "$SERVICE_FILE_SOURCE" "$SERVICE_FILE_TARGET"
  chown root:root "$SERVICE_FILE_TARGET"
  chmod 0644 "$SERVICE_FILE_TARGET"
  systemctl daemon-reload
  systemctl enable mimir-display.service
}

place_env() {
  if [[ ! -f "$ETC_DIR/.env" ]]; then
    info "Creating default env file"
    cat > "$ETC_DIR/.env" <<EOF
# Mimir Display Client Environment
# MQTT_BROKER_HOST=broker
# MQTT_BROKER_PORT=1883
# DISPLAY_ID=auto
# MIMIR_STATE_DIR=$WORK_DIR/$DEFAULT_STATE_SUBDIR
# MIMIR_CACHE_DIR=$WORK_DIR/cache
EOF
    chown $SERVICE_USER:$SERVICE_USER "$ETC_DIR/.env"
    chmod 0640 "$ETC_DIR/.env"
  fi
}

start_service() {
  systemctl restart mimir-display.service || systemctl start mimir-display.service
  systemctl --no-pager status mimir-display.service || true
}

summary() {
  echo
  info "Installation complete"
  echo "  User:        $SERVICE_USER"
  echo "  Prefix:      $PREFIX"
  echo "  Venv:        $VENV_DIR"
  echo "  Work Dir:    $WORK_DIR"
  echo "  Env File:    $ETC_DIR/.env"
  echo "  Service:     mimir-display.service"
}

main() {
  require_root
  detect_legacy_arch
  prepare_legacy_mode
  ensure_user
  create_dirs
  create_venv
  install_package
  install_service
  place_env
  start_service
  summary
  if [[ $LEGACY_MODE -eq 1 ]]; then
    echo
    info "Legacy Zero summary: system-site-packages used; ensure python3-numpy present via apt if not already."
  fi
}

main "$@"
