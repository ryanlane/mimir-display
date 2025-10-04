#!/usr/bin/env bash
set -euo pipefail

echo "=== Mimir Unified Display Installer (Interactive) ==="

if [[ $(id -u) -eq 0 ]]; then
  echo "[info] Running as root. A per-user virtualenv is recommended; proceed with caution." >&2
fi

DEFAULT_BACKEND="auto"

detect_backend() {
  # Mimics Python loader logic (lightweight subset)
  if [[ ${FORCE_INKY:-} == 1 ]]; then
    echo "inky"; return
  fi
  if [[ -e /dev/fb0 ]]; then
    local virt="$(grep -s '' /sys/class/graphics/fb0/virtual_size || true)"
    local bpp="$(grep -s '' /sys/class/graphics/fb0/bits_per_pixel || true)"
    if [[ $virt == "720,720" && ( -z $bpp || $bpp == 16 ) ]]; then
      echo "hyperpixelsq"; return
    fi
  fi
  echo "inky"
}

SUGGESTED="$(detect_backend)"

echo "Available display backends:" >&2
OPTIONS=()
LABELS=()

# Put suggested first
OPTIONS+=("$SUGGESTED")
LABELS+=("$SUGGESTED (detected)")

for opt in hyperpixelsq inky auto; do
  if [[ $opt != "$SUGGESTED" ]]; then
    OPTIONS+=("$opt")
    LABELS+=("$opt")
  fi
done

for i in "${!LABELS[@]}"; do
  printf "  %d) %s\n" "$((i+1))" "${LABELS[$i]}"
done

read -rp "Select display backend [1]: " CHOICE
CHOICE=${CHOICE:-1}
if ! [[ $CHOICE =~ ^[0-9]+$ ]] || (( CHOICE < 1 || CHOICE > ${#OPTIONS[@]} )); then
  echo "[warn] Invalid choice; defaulting to option 1 (${OPTIONS[0]})" >&2
  CHOICE=1
fi
BACKEND="${OPTIONS[$((CHOICE-1))]}"
echo "[+] Selected backend: $BACKEND" >&2

# Determine extra early (needed for armv6 numpy logic below)
EXTRA=""
case "$BACKEND" in
  inky) EXTRA="[inky]" ;;
  hyperpixelsq) EXTRA="[hyperpixelsq]" ;;
  auto) EXTRA="[all]" ;;
esac

SCRIPT_PATH="$0"
# When invoked via 'bash scripts/install_display.sh' $0 may be 'scripts/install_display.sh' or relative path.
if [[ ! -f "$SCRIPT_PATH" ]]; then
  if [[ -n ${BASH_SOURCE[0]:-} && -f ${BASH_SOURCE[0]} ]]; then
    SCRIPT_PATH="${BASH_SOURCE[0]}"
  fi
fi
PROJECT_ROOT="$(cd "$(dirname "$SCRIPT_PATH")"/.. && pwd)"

echo
echo "Installation mode:" >&2
echo "  1) Editable in-place (default)  - uses existing cloned repo (pip install -e)" >&2
echo "  2) Copy/Deploy to target path    - creates isolated install at custom directory" >&2
read -rp "Select mode [1]: " MODE_CHOICE
MODE_CHOICE=${MODE_CHOICE:-1}
if [[ $MODE_CHOICE != 1 && $MODE_CHOICE != 2 ]]; then
  echo "[warn] Invalid choice; defaulting to editable in-place" >&2
  MODE_CHOICE=1
fi

if [[ $MODE_CHOICE == 1 ]]; then
  INSTALL_DIR="$PROJECT_ROOT"
  echo "[+] Editable install selected (project root: $PROJECT_ROOT)" >&2
else
  read -rp "Install path (directory) [/opt/mimir-display]: " INSTALL_DIR
  INSTALL_DIR=${INSTALL_DIR:-/opt/mimir-display}
  # If directory requires privilege, create with sudo
  if [[ ! -d $INSTALL_DIR ]]; then
    if mkdir -p "$INSTALL_DIR" 2>/dev/null; then
      :
    else
      echo "[info] Elevated permissions required to create $INSTALL_DIR" >&2
      sudo mkdir -p "$INSTALL_DIR"
      # If script wasn't launched with sudo, SUDO_USER is empty; ensure we chown to invoking user.
      if [[ -z ${SUDO_USER:-} ]]; then
        sudo chown -R "$USER":"$USER" "$INSTALL_DIR" 2>/dev/null || true
      fi
    fi
  fi
  echo "[+] Will perform a copy/deploy install to $INSTALL_DIR" >&2
  RSYNC_CMD=(rsync -a --delete --exclude '.venv' --exclude '.git')
  if [[ -w $INSTALL_DIR ]]; then
    "${RSYNC_CMD[@]}" "$PROJECT_ROOT/" "$INSTALL_DIR/"
  else
    echo "[info] Using sudo for rsync into $INSTALL_DIR" >&2
    sudo "${RSYNC_CMD[@]}" "$PROJECT_ROOT/" "$INSTALL_DIR/"
    # Adjust ownership to invoking (non-root) user if SUDO_USER exists
    if [[ -n ${SUDO_USER:-} ]]; then
      sudo chown -R "$SUDO_USER":"$SUDO_USER" "$INSTALL_DIR" 2>/dev/null || true
    else
      # Fallback: chown to current user when script itself invoked without sudo but needs sudo internally
      sudo chown -R "$USER":"$USER" "$INSTALL_DIR" 2>/dev/null || true
    fi
  fi
fi

cd "$INSTALL_DIR"

# ------------------------------------------------------------
# Platform / architecture helpers (for Pi Zero W armv6 issues)
# ------------------------------------------------------------
ARCH="$(uname -m 2>/dev/null || echo unknown)"
USE_SYSTEM_NUMPY=0
if [[ $ARCH == armv6l ]]; then
  echo "[info] Detected ARMv6 (e.g. Raspberry Pi Zero W)." >&2
  if [[ $BACKEND == inky || $EXTRA == *inky* ]]; then
    echo "[warn] Inky backend pulls numpy; armv6 often lacks working wheels." >&2
    echo "      Option: use system-packaged numpy + openblas via --system-site-packages venv." >&2
    read -rp "Install system numpy/openblas & use system-site-packages venv? (Y/n): " SYSNP
    SYSNP=${SYSNP:-Y}
    if [[ ${SYSNP,,} != n* ]]; then
      USE_SYSTEM_NUMPY=1
      echo "[+] Will install system numeric libs and create venv with --system-site-packages." >&2
      sudo apt update
      sudo apt install -y python3-numpy libopenblas0 || true
    else
      echo "[info] Proceeding without system numpy; may hit build/import errors." >&2
    fi
  fi
fi

# Pillow build deps (optional) if building from source likely
if [[ $ARCH == armv6l ]]; then
  read -rp "Install Pillow build dependencies (zlib, jpeg, freetype etc)? (Y/n): " PILDEPS
  PILDEPS=${PILDEPS:-Y}
  if [[ ${PILDEPS,,} != n* ]]; then
    echo "[+] Installing Pillow build deps" >&2
    sudo apt update
    sudo apt install -y zlib1g-dev libjpeg62-turbo-dev libtiff5-dev libopenjp2-7-dev \
      libfreetype6-dev liblcms2-dev libwebp-dev libharfbuzz-dev libfribidi-dev libxcb1-dev || true
  fi
fi

if [[ ! -d .venv ]]; then
  echo "[+] Creating virtualenv (.venv)" >&2
  if [[ $USE_SYSTEM_NUMPY == 1 ]]; then
    python3 -m venv --system-site-packages .venv
  else
    python3 -m venv .venv
  fi
fi
source .venv/bin/activate
pip install --upgrade pip setuptools wheel


if [[ ! -f "pyproject.toml" ]]; then
  echo "[error] pyproject.toml not found in $INSTALL_DIR; aborting." >&2
  exit 1
fi

if [[ $MODE_CHOICE == 1 ]]; then
  echo "[+] Editable install: pip install -e .${EXTRA}" >&2
  pip install -e ".$EXTRA"
else
  echo "[+] Standard install from copied tree .${EXTRA}" >&2
  pip install ".$EXTRA"
fi

ENV_FILE=".env"
if [[ -f $ENV_FILE ]]; then
  echo "[info] Existing .env found; will append DISPLAY_BACKEND key if absent"
else
  echo "[+] Creating .env"
  touch $ENV_FILE
fi
grep -q '^DISPLAY_BACKEND=' .env || echo "DISPLAY_BACKEND=${BACKEND}" >> .env
grep -q '^LOG_LEVEL=' .env || echo "LOG_LEVEL=INFO" >> .env

# ----------------------------------------------
# Orientation prompt (new)
# ----------------------------------------------
read -rp "Display orientation (landscape|portrait_left|portrait_right) [landscape]: " ORIENTATION_INPUT || true
ORIENTATION_INPUT=${ORIENTATION_INPUT:-landscape}
case "$ORIENTATION_INPUT" in
  landscape|portrait_left|portrait_right) : ;; 
  *) echo "[warn] Invalid orientation '$ORIENTATION_INPUT'; defaulting to landscape" >&2; ORIENTATION_INPUT=landscape ;;
esac
if grep -q '^DISPLAY_ORIENTATION=' .env; then
  sed -i "s/^DISPLAY_ORIENTATION=.*/DISPLAY_ORIENTATION=${ORIENTATION_INPUT}/" .env
else
  echo "DISPLAY_ORIENTATION=${ORIENTATION_INPUT}" >> .env
fi
echo "[+] Orientation set to ${ORIENTATION_INPUT} (rotate content automatically)" >&2

if [[ $BACKEND == "hyperpixelsq" ]]; then
  # Attempt to detect Raspberry Pi boot config location (varies between distros)
  BOOT_CANDIDATES=(/boot/firmware/config.txt /boot/config.txt)
  CONFIG_PATH=""
  for p in "${BOOT_CANDIDATES[@]}"; do
    if [[ -f $p ]]; then
      CONFIG_PATH=$p
      break
    fi
  done
  if [[ -n $CONFIG_PATH ]]; then
    OVERLAY_LINE="dtoverlay=vc4-kms-dpi-hyperpixel4sq"
    if ! grep -q "^${OVERLAY_LINE}" "$CONFIG_PATH"; then
      echo "[info] HyperPixel overlay not found in $CONFIG_PATH"
      read -rp "Append '${OVERLAY_LINE}' to $CONFIG_PATH now? (y/N): " ADD_OVL
      if [[ ${ADD_OVL,,} == y* ]]; then
        echo "[+] Adding overlay line to $CONFIG_PATH (requires reboot)"
        sudo tee -a "$CONFIG_PATH" >/dev/null <<<"${OVERLAY_LINE}"
        echo "[info] Overlay appended. Reboot after install to activate framebuffer."
      else
        echo "[warn] Overlay not added. Ensure '${OVERLAY_LINE}' is present before running service." >&2
      fi
    else
      echo "[info] HyperPixel overlay already present in $CONFIG_PATH"
    fi
  else
    echo "[warn] Could not locate config.txt to verify HyperPixel overlay. Check your boot partition manually." >&2
  fi
fi

read -rp "Create systemd service? (y/N): " MAKE_SVC
if [[ ${MAKE_SVC,,} == y* ]]; then
  SERVICE_PATH="/etc/systemd/system/mimir-display.service"
  echo "[+] Writing $SERVICE_PATH"
  cat <<SERVICE | sudo tee "$SERVICE_PATH" >/dev/null
[Unit]
Description=Mimir Unified Display Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python -m mimir_display --backend ${BACKEND}
Restart=on-failure
RestartSec=3
User=${SUDO_USER:-$USER}
Group=video

[Install]
WantedBy=multi-user.target
SERVICE
  sudo systemctl daemon-reload
  sudo systemctl enable mimir-display.service
  echo "[+] Service installed. Start with: sudo systemctl start mimir-display"
fi

echo "=== Install complete ==="
echo "Activate with: source $INSTALL_DIR/.venv/bin/activate && mimir-display --backend $BACKEND"