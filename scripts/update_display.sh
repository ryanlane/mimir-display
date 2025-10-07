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
#   SKIP_PROJECT_INSTALL  Set to 1 (or pass --skip-install) to skip the editable project install ONLY
#                         while still upgrading pip/setuptools (unless SKIP_PIP=1 as well).
#
# Exit codes:
#   0 success, non-zero on failure.
#

git fetch && git pull

color() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info() { echo "$(color 36 [INFO]) $*"; }
warn() { echo "$(color 33 [WARN]) $*"; }
err()  { echo "$(color 31 [ERR])  $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INSTALL_DIR_DEFAULT="/opt/mimir-display"
SERVICE_NAME="${SERVICE_NAME:-mimir-display}"
INSTALL_DIR="${INSTALL_DIR:-}"  # user override
SKIP_PROJECT_INSTALL="${SKIP_PROJECT_INSTALL:-0}"  # allow env default

# Parse simple CLI flags (no long getopts overhead)
for arg in "$@"; do
  case "$arg" in
    --skip-install|--skip-project-install)
      SKIP_PROJECT_INSTALL=1 ;;
  esac
done

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
  # --- Environment file preservation ---------------------------------------
  # We NEVER want to lose a deployment's live .env by virtue of --delete.
  # Default: preserve existing .env (exclude from rsync) and create timestamped backups.
  # Set ALLOW_ENV_OVERWRITE=1 to let rsync replace it (still backed up first).
  PRESERVE_ENV=${PRESERVE_ENV:-1}
  ALLOW_ENV_OVERWRITE=${ALLOW_ENV_OVERWRITE:-0}
  ENV_PATH="$INSTALL_DIR/.env"
  if [[ -f $ENV_PATH ]]; then
    ts=$(date +%Y%m%d-%H%M%S)
    cp "$ENV_PATH" "$INSTALL_DIR/.env.backup-$ts"
    info "Backed up existing .env -> .env.backup-$ts"
  fi
  RSYNC_EXCLUDES=(--exclude .venv --exclude .git --exclude __pycache__)
  if [[ $ALLOW_ENV_OVERWRITE != 1 ]]; then
    RSYNC_EXCLUDES+=(--exclude .env)
  fi
  rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$REPO_ROOT/" "$INSTALL_DIR/"
  # If overwrite allowed and repo lacks .env, restore from backup to avoid accidental purge.
  if [[ $ALLOW_ENV_OVERWRITE == 1 && ! -f $ENV_PATH ]]; then
    latest_backup=$(ls -1t "$INSTALL_DIR"/.env.backup-* 2>/dev/null | head -n1 || true)
    if [[ -n $latest_backup ]]; then
      cp "$latest_backup" "$ENV_PATH"
      warn ".env missing in repo; restored from $latest_backup"
    else
      warn ".env missing after sync and no backup found; create one manually."
    fi
  fi
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
  if [[ ${SKIP_PROJECT_INSTALL:-0} == 1 ]]; then
    info "Skipping project editable install per SKIP_PROJECT_INSTALL flag"
  else
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
  fi
else
  info "Skipping pip install per SKIP_PIP=1"
fi

# Systemd reload + restart if unit exists (robust detection)
service_unit="${SERVICE_NAME}.service"
if systemctl status "$service_unit" >/dev/null 2>&1 || systemctl is-active --quiet "$service_unit" || \
   systemctl list-units --all --full | grep -Fq "$service_unit"; then
  info "Restarting service $service_unit"
  if ! sudo systemctl restart "$service_unit"; then
    err "Service restart failed"
    # Provide diagnostic hints
    systemctl status "$service_unit" || true
    exit 4
  fi
  sleep 1
  systemctl --no-pager --lines=8 status "$service_unit" || true
else
  warn "Service $service_unit not detected via status/is-active. Skipping restart."
  info "Debug: listing matching unit files (may be static/disabled):"
  systemctl list-unit-files | grep -F "$service_unit" || true
fi

info "Update complete"
