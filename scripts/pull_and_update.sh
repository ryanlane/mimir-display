#!/usr/bin/env bash
set -euo pipefail
#
# pull_and_update.sh
#
# Fetch latest code from git and run the standard update script.
# All arguments are forwarded to update_display.sh.
#
# Called by:
#   - mimir-display-updater.timer  (scheduled automatic updates)
#   - POST /update-client webhook  (on-demand via HTTP)
#   - MQTT update_client command   (on-demand via server UI / MQTT)
#
# Environment variables (forwarded to update_display.sh):
#   GIT_REMOTE    Remote name to pull from       (default: origin)
#   GIT_BRANCH    Branch to pull                 (default: main)
#   All update_display.sh variables apply too (DRY_RUN, SKIP_RESTART, etc.)
#

color() { printf "\033[%sm%s\033[0m" "$1" "$2"; }
info() { echo "$(color 36 [INFO]) $*"; }
warn() { echo "$(color 33 [WARN]) $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"

info "Pulling latest code from $GIT_REMOTE/$GIT_BRANCH"
cd "$REPO_ROOT"

# Fetch quietly; abort if git is not available or not a repo
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  warn "Not inside a git repository; skipping pull (running update only)"
else
  git fetch --quiet "$GIT_REMOTE" || warn "git fetch failed (network issue?); continuing with local code"
  git pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH" || {
    warn "git pull failed (diverged history or conflict?); continuing with local code"
  }
  GIT_REV=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
  info "Now at $GIT_REV"
fi

info "Running update_display.sh"
exec "$SCRIPT_DIR/update_display.sh" "$@"
