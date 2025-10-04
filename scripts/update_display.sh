#!/usr/bin/env bash
set -euo pipefail
#
# Mimir Display Update Script
#
# Purpose: Deploy updated code to an existing installation and restart the
#          mimir-display systemd service.
#
# Supports two deployment models:
#   1. Editable install (repo itself is the working directory) - just reinstall deps.
#   2. Copied/Deployed install (e.g. /opt/mimir-display) - rsync new code in then reinstall.
#
# Usage examples:
#   ./scripts/update_display.sh                 # auto-detect
#   INSTALL_DIR=/opt/mimir-display ./scripts/update_display.sh
#   SERVICE_NAME=mimir-display ./scripts/update_display.sh
#   FORCE_SYNC=1 ./scripts/update_display.sh    # force rsync even if git root == install dir
#
# Environment variables:
#   INSTALL_DIR   Target installation directory (default: /opt/mimir-display if exists, else repo root)
#   SERVICE_NAME  systemd unit name (default: mimir-display)
#   FORCE_SYNC    When set to 1 forces rsync copy regardless of heuristic.
#   PIP_EXTRAS    Set to "[hyperpixelsq]" / "[inky]" / "[all]" to override extras.
#   SKIP_PIP      Set to 1 to skip pip install step.
#
# Exit codes:
#   0 success, non-zero on failure.
#

color() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info() { echo "$(color 36 [INFO]) $*"; }
warn() { echo "$(color 33 [WARN]) $*"; }
err()  { echo "$(color 31 [ERR])  $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR_DEFAULT="/opt/mimir-display"
SERVICE_NAME="${SERVICE_NAME:-mimir-display}"
INSTALL_DIR="${INSTALL_DIR:-}"  # user override

if [[ -z $INSTALL_DIR ]]; then
  if [[ -d $INSTALL_DIR_DEFAULT && -f $INSTALL_DIR_DEFAULT/.env ]]; then
    INSTALL_DIR="$INSTALL_DIR_DEFAULT"
  else
    INSTALL_DIR="$REPO_ROOT"
  fi
fi

info "Repo root: $REPO_ROOT"
info "Install dir: $INSTALL_DIR"
info "Service: $SERVICE_NAME"

if [[ ! -d $INSTALL_DIR ]]; then
  err "Install directory does not exist: $INSTALL_DIR"
  exit 2
fi

# Detect if editable (same directory) vs deployed copy
NEED_SYNC=0
if [[ ${FORCE_SYNC:-0} == 1 ]]; then
  NEED_SYNC=1
else
  if [[ "$REPO_ROOT" != "$INSTALL_DIR" ]]; then
    NEED_SYNC=1
  fi
fi

if [[ $NEED_SYNC == 1 ]]; then
  info "Synchronizing source -> install (rsync)"
  RSYNC_EXCLUDES=(--exclude .venv --exclude .git --exclude __pycache__) 
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$REPO_ROOT/" "$INSTALL_DIR/"
else
  info "Editable install detected (same directory); skipping rsync"
fi

cd "$INSTALL_DIR"

# Virtualenv detection / creation
if [[ -d .venv ]]; then
  PYBIN=".venv/bin/python"
else
  warn ".venv not found; creating"
  python3 -m venv .venv
  PYBIN=".venv/bin/python"
fi

if [[ ! -x $PYBIN ]]; then
  err "Python interpreter missing in venv: $PYBIN"; exit 3; fi

# Optionally upgrade / reinstall
if [[ ${SKIP_PIP:-0} != 1 ]]; then
  info "Upgrading pip tooling"
  $PYBIN -m pip install --upgrade pip wheel setuptools >/dev/null
  EXTRAS="${PIP_EXTRAS:-}"
  if [[ -f pyproject.toml ]]; then
    if [[ -n $EXTRAS ]]; then
      info "Installing package with extras $EXTRAS"
      $PYBIN -m pip install -e ".${EXTRAS}" >/dev/null
    else
      info "Installing package (no extras override)"
      $PYBIN -m pip install -e . >/dev/null
    fi
  else
    warn "pyproject.toml missing; skipping package install"
  fi
else
  info "Skipping pip install per SKIP_PIP=1"
fi

# Systemd reload + restart if unit exists
if systemctl list-units --type=service --all | grep -q "${SERVICE_NAME}.service"; then
  info "Restarting service ${SERVICE_NAME}.service"
  sudo systemctl restart "${SERVICE_NAME}.service" || { err "Service restart failed"; exit 4; }
  sleep 1
  systemctl --no-pager --lines=5 status "${SERVICE_NAME}.service" || true
else
  warn "Service ${SERVICE_NAME}.service not found; skipping restart"
fi

info "Update complete"
