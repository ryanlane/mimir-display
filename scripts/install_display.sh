#!/usr/bin/env bash
set -euo pipefail

echo "=== Mimir Unified Display Installer ==="

if [[ $(id -u) -eq 0 ]]; then
  echo "[info] Running as root. A per-user virtualenv is recommended; proceed with caution." >&2
fi

DEFAULT_BACKEND="auto"
read -rp "Select backend (inky/hyperpixelsq/auto) [auto]: " CHOICE
BACKEND=${CHOICE:-$DEFAULT_BACKEND}
if [[ ! $BACKEND =~ ^(inky|hyperpixelsq|auto)$ ]]; then
  echo "Invalid backend choice" >&2; exit 1
fi

read -rp "Install path (directory) [/opt/mimir-display]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/opt/mimir-display}
mkdir -p "$INSTALL_DIR"

cd "$INSTALL_DIR"

if [[ ! -d .venv ]]; then
  echo "[+] Creating virtualenv (.venv)"
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip

EXTRA=""
case "$BACKEND" in
  inky) EXTRA="[inky]" ;;
  hyperpixelsq) EXTRA="[hyperpixelsq]" ;;
  auto) EXTRA="[all]" ;;
esac

echo "[+] Installing mimir-display$EXTRA"
pip install mimir-display$EXTRA

ENV_FILE=".env"
if [[ -f $ENV_FILE ]]; then
  echo "[info] Existing .env found; will append DISPLAY_BACKEND key if absent"
else
  echo "[+] Creating .env"
  touch $ENV_FILE
fi
grep -q '^DISPLAY_BACKEND=' .env || echo "DISPLAY_BACKEND=${BACKEND}" >> .env
grep -q '^LOG_LEVEL=' .env || echo "LOG_LEVEL=INFO" >> .env

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