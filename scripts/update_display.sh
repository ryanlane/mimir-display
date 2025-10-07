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
#   DRY_RUN=1 ./scripts/update_display.sh       # show actions; perform no changes
#   SKIP_RESTART=1 ./scripts/update_display.sh  # deploy but do not restart systemd unit
#   HEALTH_CHECK=1 ./scripts/update_display.sh  # run --health after restart
#
# Environment variables:
#   INSTALL_DIR   Target installation directory (default: /opt/mimir-display if exists, else repo root)
#   SERVICE_NAME  systemd unit name (default: mimir-display)
#   FORCE_SYNC    When set to 1 forces rsync copy regardless of heuristic.
#   PIP_EXTRAS    Set to "[hyperpixelsq]" / "[inky]" / "[all]" to override extras.
#   SKIP_PIP      Set to 1 to skip pip install step.
#   SKIP_PROJECT_INSTALL  Set to 1 (or pass --skip-install) to skip the editable project install ONLY
#                         while still upgrading pip/setuptools (unless SKIP_PIP=1 as well).
#   DRY_RUN       If 1, perform no mutating actions (prints what would happen).
#   SKIP_RESTART  If 1, do not restart the systemd service after deployment.
#   HEALTH_CHECK  If 1, invoke the client with --health and report status.
#   VERSION_FILE  Optional file (default: .deploy-version) to write git short SHA.
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
SKIP_PROJECT_INSTALL="${SKIP_PROJECT_INSTALL:-0}"  # allow env default
DRY_RUN="${DRY_RUN:-0}"
SKIP_RESTART="${SKIP_RESTART:-0}"
HEALTH_CHECK="${HEALTH_CHECK:-0}"
VERSION_FILE="${VERSION_FILE:-.deploy-version}"

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
info "Dry run: $DRY_RUN  Skip restart: $SKIP_RESTART  Health check: $HEALTH_CHECK"

# Check git status if we're in a git repo
if [[ -d "$REPO_ROOT/.git" ]]; then
  cd "$REPO_ROOT"
  # Check if local branch is behind remote
  git fetch --dry-run 2>/dev/null || warn "Could not fetch from remote (network/auth issue?)"
  
  LOCAL=$(git rev-parse @ 2>/dev/null || echo "")
  REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "")
  BASE=$(git merge-base @ @{u} 2>/dev/null || echo "")
  
  if [[ -n "$LOCAL" && -n "$REMOTE" && -n "$BASE" ]]; then
    if [[ "$LOCAL" != "$REMOTE" ]]; then
      if [[ "$LOCAL" == "$BASE" ]]; then
        warn "Local branch is behind remote. Consider running 'git pull' first."
      elif [[ "$REMOTE" == "$BASE" ]]; then
        warn "Local branch has unpushed commits."
      else
        warn "Local and remote branches have diverged."
      fi
    else
      info "Git status: up to date with remote"
    fi
  fi
fi

# Detect if running as root (needed for rgbmatrix GPIO) and record service user (if configured)
SERVICE_USER=$(systemctl show "${SERVICE_NAME}.service" -p User 2>/dev/null | awk -F= '{print $2}' || true)
SERVICE_USER=${SERVICE_USER:-root}
EUID_ACTUAL=$(id -u)
if [[ $EUID_ACTUAL -ne 0 && "$SERVICE_USER" == "root" ]]; then
  warn "Service runs as root but script not executed with root privileges; will prefix privileged ops with sudo."
fi

maybe_sudo() { if [[ $EUID_ACTUAL -ne 0 ]]; then command sudo "$@"; else "$@"; fi }
maybe_sudo_root_target() { # Used for actions affecting INSTALL_DIR when service expects root ownership
  if [[ $SERVICE_USER == root && $EUID_ACTUAL -ne 0 ]]; then sudo "$@"; else "$@"; fi }

# Fix permissions for directories when service runs as root
if [[ "$SERVICE_USER" == "root" ]]; then
  info "Ensuring proper root ownership for service directories"
  if [[ -d "/var/lib/mimir-display" ]]; then
    if [[ $DRY_RUN == 0 ]]; then
      maybe_sudo chown -R root:root /var/lib/mimir-display
    else
      echo "DRY_RUN: chown -R root:root /var/lib/mimir-display"
    fi
  fi
fi

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
  info "Stopping service before sync (if running)"
  if [[ $DRY_RUN == 0 ]]; then maybe_sudo systemctl stop "${SERVICE_NAME}.service" 2>/dev/null || true; else echo "DRY_RUN: systemctl stop ${SERVICE_NAME}.service"; fi
  
  # Fix ownership before attempting any file operations on INSTALL_DIR
  if [[ "$SERVICE_USER" == "root" && -d "$INSTALL_DIR" && "$INSTALL_DIR" != "$REPO_ROOT" ]]; then
    info "Ensuring install directory ownership for sync operations"
    if [[ $DRY_RUN == 0 ]]; then
      maybe_sudo chown -R root:root "$INSTALL_DIR"
    else
      echo "DRY_RUN: chown -R root:root $INSTALL_DIR"
    fi
  fi
  
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
    if [[ $DRY_RUN == 0 ]]; then cp "$ENV_PATH" "$INSTALL_DIR/.env.backup-$ts"; else echo "DRY_RUN: cp $ENV_PATH $INSTALL_DIR/.env.backup-$ts"; fi
    info "Backed up existing .env -> .env.backup-$ts"
  fi
  RSYNC_EXCLUDES=(--exclude .venv --exclude .git --exclude __pycache__)
  if [[ $ALLOW_ENV_OVERWRITE != 1 ]]; then
    RSYNC_EXCLUDES+=(--exclude .env)
  fi
  if [[ $DRY_RUN == 0 ]]; then
    maybe_sudo_root_target rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$REPO_ROOT/" "$INSTALL_DIR/"
  else
    echo "DRY_RUN: rsync -a --delete ${RSYNC_EXCLUDES[*]} $REPO_ROOT/ $INSTALL_DIR/"
  fi
  # If overwrite allowed and repo lacks .env, restore from backup to avoid accidental purge.
  if [[ $ALLOW_ENV_OVERWRITE == 1 && ! -f $ENV_PATH ]]; then
    latest_backup=$(ls -1t "$INSTALL_DIR"/.env.backup-* 2>/dev/null | head -n1 || true)
    if [[ -n $latest_backup ]]; then
      if [[ $DRY_RUN == 0 ]]; then cp "$latest_backup" "$ENV_PATH"; else echo "DRY_RUN: cp $latest_backup $ENV_PATH"; fi
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
  if [[ $DRY_RUN == 0 ]]; then python3 -m venv .venv; else echo "DRY_RUN: python3 -m venv .venv"; fi
  PYBIN=".venv/bin/python"
fi

if [[ ! -x $PYBIN && $DRY_RUN == 0 ]]; then
  err "Python interpreter missing in venv: $PYBIN"; exit 3; fi

# Optionally upgrade / reinstall
if [[ ${SKIP_PIP:-0} != 1 ]]; then
  info "Upgrading pip tooling"
  if [[ $DRY_RUN == 0 ]]; then $PYBIN -m pip install --upgrade pip wheel setuptools >/dev/null; else echo "DRY_RUN: $PYBIN -m pip install --upgrade pip wheel setuptools"; fi
  EXTRAS="${PIP_EXTRAS:-}"
  if [[ ${SKIP_PROJECT_INSTALL:-0} == 1 ]]; then
    info "Skipping project editable install per SKIP_PROJECT_INSTALL flag"
  else
    if [[ -f pyproject.toml ]]; then
      if [[ -n $EXTRAS ]]; then
        info "Installing package with extras $EXTRAS"
        if [[ $DRY_RUN == 0 ]]; then $PYBIN -m pip install -e ".${EXTRAS}" >/dev/null; else echo "DRY_RUN: $PYBIN -m pip install -e .${EXTRAS}"; fi
      else
        info "Installing package (no extras override)"
        if [[ $DRY_RUN == 0 ]]; then $PYBIN -m pip install -e . >/dev/null; else echo "DRY_RUN: $PYBIN -m pip install -e ."; fi
      fi
    else
      warn "pyproject.toml missing; skipping package install"
    fi
  fi
else
  info "Skipping pip install per SKIP_PIP=1"
fi

# Version stamping (write current git short SHA)
GIT_REV=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
if [[ $DRY_RUN == 0 ]]; then echo "$GIT_REV" > "$VERSION_FILE"; else echo "DRY_RUN: echo $GIT_REV > $VERSION_FILE"; fi
info "Version stamp: $GIT_REV (stored in $VERSION_FILE)"

# Verify ExecStart points to venv when using deployed install
if [[ $INSTALL_DIR == /opt/mimir-display ]]; then
  EXEC_LINE=$(systemctl show "${SERVICE_NAME}.service" -p ExecStart 2>/dev/null | sed 's/ExecStart=//') || true
  if [[ -n $EXEC_LINE ]]; then
    if [[ $EXEC_LINE != *"$INSTALL_DIR/.venv/bin/python"* ]]; then
      warn "ExecStart does not reference $INSTALL_DIR/.venv/bin/python -> $EXEC_LINE"
      warn "Consider updating unit to: ExecStart=$INSTALL_DIR/.venv/bin/python -m mimir_display"
    else
      info "ExecStart uses venv interpreter (good)"
    fi
  else
    warn "Could not retrieve ExecStart for service (permissions?)"
  fi
fi

# Systemd reload + restart if unit exists (robust detection)
service_unit="${SERVICE_NAME}.service"
if [[ $SKIP_RESTART == 1 ]]; then
  info "SKIP_RESTART=1 set; not restarting $service_unit"
else
  if systemctl status "$service_unit" >/dev/null 2>&1 || systemctl is-active --quiet "$service_unit" || \
     systemctl list-units --all --full | grep -Fq "$service_unit"; then
    info "Restarting service $service_unit"
    if [[ $DRY_RUN == 0 ]]; then
      if ! maybe_sudo systemctl restart "$service_unit"; then
        err "Service restart failed"
        systemctl status "$service_unit" || true
        exit 4
      fi
    else
      echo "DRY_RUN: systemctl restart $service_unit"
    fi
    sleep 1
    systemctl --no-pager --lines=8 status "$service_unit" || true
  else
    warn "Service $service_unit not detected via status/is-active. Skipping restart."
    info "Debug: listing matching unit files (may be static/disabled):"
    systemctl list-unit-files | grep -F "$service_unit" || true
  fi
fi

# Optional health check (uses venv python) – treat non-zero/ degraded as warning
if [[ $HEALTH_CHECK == 1 && $DRY_RUN == 0 ]]; then
  if [[ -x $PYBIN ]]; then
    info "Running post-restart health check"
    set +e
    HC_OUT=$($PYBIN -m mimir_display --health 2>&1)
    HC_CODE=$?
    set -e
    echo "$HC_OUT"
    if [[ $HC_CODE -ne 0 ]]; then
      warn "Health check exit code $HC_CODE (see output above)"
    else
      info "Health check OK"
    fi
  else
    warn "Cannot run health check; interpreter missing: $PYBIN"
  fi
elif [[ $HEALTH_CHECK == 1 ]]; then
  echo "DRY_RUN: would run health check"
fi

info "Update complete"
