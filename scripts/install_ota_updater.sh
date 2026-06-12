#!/usr/bin/env bash
set -euo pipefail
#
# Install/refresh the root OTA updater units on a display (idempotent).
# Called by update_display.sh on every deploy; can also be run manually:
#   sudo bash scripts/install_ota_updater.sh
#
# After this is in place the display self-updates: the client writes
# /var/lib/mimir-display/ota/request.json and the path unit fires
# ota_update.sh as root (A/B install, health check, rollback).

INSTALL_DIR="${INSTALL_DIR:-/opt/mimir-display}"
OTA_DIR="${OTA_DIR:-/var/lib/mimir-display/ota}"
SERVICE_NAME="${SERVICE_NAME:-mimir-display}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SUDO=""
[ "$(id -u)" -ne 0 ] && SUDO="sudo"

# OTA work dir: client (service user) writes request.json; root writes status.json
SERVICE_USER=$($SUDO systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || true)
SERVICE_USER="${SERVICE_USER:-pi}"
$SUDO mkdir -p "$OTA_DIR"
$SUDO chown "$SERVICE_USER" "$OTA_DIR"
$SUDO chmod 755 "$OTA_DIR"

$SUDO cp "$SCRIPT_DIR/mimir-display-updater.service" /etc/systemd/system/
$SUDO cp "$SCRIPT_DIR/mimir-display-updater.path" /etc/systemd/system/
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now mimir-display-updater.path

echo "[ota-install] updater units installed; watching $OTA_DIR/request.json"
