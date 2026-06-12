#!/usr/bin/env bash
set -euo pipefail
#
# Mimir Display OTA updater — runs as ROOT, triggered by
# mimir-display-updater.path when the client writes
# /var/lib/mimir-display/ota/request.json (see mimir_display/ota.py).
#
# A/B install layout:
#   /opt/mimir-display/releases/v1.0.4/   (artifact + its own .venv)
#   /opt/mimir-display/current -> releases/v1.0.4
#
# Steps: download -> sha256 verify -> venv install -> health check ->
#        flip symlink -> restart service -> verify -> status.json
# Any failure leaves the previous version running and reports
# result=failed; the client backs off and retries hourly.

INSTALL_DIR="${INSTALL_DIR:-/opt/mimir-display}"
OTA_DIR="${OTA_DIR:-/var/lib/mimir-display/ota}"
SERVICE_NAME="${SERVICE_NAME:-mimir-display}"
RELEASES_DIR="$INSTALL_DIR/releases"
CURRENT_LINK="$INSTALL_DIR/current"
DROPIN_DIR="/etc/systemd/system/${SERVICE_NAME}.service.d"
KEEP_RELEASES=2
LOCK_FILE="$OTA_DIR/.lock"

log() { echo "[ota] $*"; }

write_status() { # result target_version [error]
  local tmp="$OTA_DIR/status.json.tmp"
  python3 - "$1" "$2" "${3:-}" > "$tmp" <<'EOF'
import json, sys, datetime
result, target, error = sys.argv[1], sys.argv[2], sys.argv[3]
out = {
    "result": result,
    "target_version": target,
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
if error:
    out["error"] = error
print(json.dumps(out, indent=2))
EOF
  mv "$tmp" "$OTA_DIR/status.json"
  chmod 644 "$OTA_DIR/status.json"
}

fail() { # target_version message
  log "FAILED: $2"
  write_status failed "$1" "$2"
  exit 1
}

# ---------- read + consume request ----------
REQUEST="$OTA_DIR/request.json"
[ -f "$REQUEST" ] || { log "no request file — nothing to do"; exit 0; }

exec 9>"$LOCK_FILE"
flock -n 9 || { log "another update is in progress"; exit 0; }

VERSION=$(python3 -c "import json;print(json.load(open('$REQUEST'))['version'])")
DOWNLOAD_URL=$(python3 -c "import json;print(json.load(open('$REQUEST'))['download_url'])")
SHA256=$(python3 -c "import json;print(json.load(open('$REQUEST'))['sha256'])")
ARTIFACT=$(python3 -c "import json;print(json.load(open('$REQUEST')).get('artifact') or 'mimir_display-$VERSION.tar.gz')")
rm -f "$REQUEST"   # consume: a bad request must not retrigger forever

log "update requested -> v$VERSION ($DOWNLOAD_URL)"
write_status in_progress "$VERSION"

# ---------- service user + pip extras ----------
SERVICE_USER=$(systemctl show -p User --value "$SERVICE_NAME" 2>/dev/null || true)
SERVICE_USER="${SERVICE_USER:-pi}"

# Extras: explicit OTA_PIP_EXTRAS in the device .env wins; else map from backend.
ENV_FILE="$INSTALL_DIR/.env"
EXTRAS=$(grep -E '^OTA_PIP_EXTRAS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
if [ -z "$EXTRAS" ]; then
  BACKEND=$(grep -E '^DISPLAY_BACKEND=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
  case "$BACKEND" in
    inky)         EXTRAS="inky" ;;
    hyperpixelsq) EXTRAS="hyperpixelsq" ;;
    hdmi)         EXTRAS="hdmi" ;;
    rgbmatrix)    EXTRAS="rgbmatrix" ;;
    *)            EXTRAS="all" ;;
  esac
fi
log "service_user=$SERVICE_USER extras=[$EXTRAS]"

# ---------- download + verify ----------
VERSION_DIR="$RELEASES_DIR/v$VERSION"
mkdir -p "$VERSION_DIR"
ARTIFACT_PATH="$VERSION_DIR/$ARTIFACT"
curl -fsSL -o "$ARTIFACT_PATH" "$DOWNLOAD_URL" || fail "$VERSION" "download failed: $DOWNLOAD_URL"

ACTUAL_SHA=$(sha256sum "$ARTIFACT_PATH" | cut -d' ' -f1)
[ "$ACTUAL_SHA" = "$SHA256" ] || fail "$VERSION" "sha256 mismatch (expected $SHA256 got $ACTUAL_SHA)"
log "artifact verified"

# ---------- venv install ----------
if [ ! -x "$VERSION_DIR/.venv/bin/python" ]; then
  python3 -m venv "$VERSION_DIR/.venv" || fail "$VERSION" "venv creation failed"
fi
"$VERSION_DIR/.venv/bin/pip" install --upgrade pip wheel >/dev/null 2>&1 || true
"$VERSION_DIR/.venv/bin/pip" install "mimir-display[$EXTRAS] @ file://$ARTIFACT_PATH" \
  || fail "$VERSION" "pip install failed"
log "installed into $VERSION_DIR/.venv"

# ---------- health check (as service user; hardware probing) ----------
# exit codes: 0 ok, 1 degraded (acceptable), >=2 error
set +e
runuser -u "$SERVICE_USER" -- "$VERSION_DIR/.venv/bin/mimir-display" --health >/dev/null 2>&1
HEALTH_RC=$?
set -e
if [ "$HEALTH_RC" -ge 2 ]; then
  fail "$VERSION" "health check failed (rc=$HEALTH_RC)"
elif [ "$HEALTH_RC" -eq 1 ]; then
  log "health check degraded (rc=$HEALTH_RC) — proceeding"
else
  log "health check OK"
fi

# ---------- flip symlink + systemd drop-in ----------
PREVIOUS_TARGET=""
[ -L "$CURRENT_LINK" ] && PREVIOUS_TARGET=$(readlink "$CURRENT_LINK")
ln -sfn "$VERSION_DIR" "$CURRENT_LINK"

DROPIN_CREATED=0
if [ ! -f "$DROPIN_DIR/ota.conf" ]; then
  mkdir -p "$DROPIN_DIR"
  cat > "$DROPIN_DIR/ota.conf" <<EOF
# Managed by ota_update.sh — points the service at the OTA 'current' symlink.
[Service]
ExecStart=
ExecStart=$CURRENT_LINK/.venv/bin/python -m mimir_display
WorkingDirectory=$INSTALL_DIR
EOF
  DROPIN_CREATED=1
  log "systemd drop-in installed (service now follows $CURRENT_LINK)"
fi
systemctl daemon-reload

# ---------- restart + verify ----------
rollback() {
  log "rolling back..."
  if [ -n "$PREVIOUS_TARGET" ]; then
    ln -sfn "$PREVIOUS_TARGET" "$CURRENT_LINK"
  elif [ "$DROPIN_CREATED" = "1" ]; then
    rm -f "$DROPIN_DIR/ota.conf"   # first OTA: fall back to the legacy unit paths
  fi
  systemctl daemon-reload
  systemctl restart "$SERVICE_NAME" || true
}

systemctl restart "$SERVICE_NAME"
sleep 8
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
  rollback
  fail "$VERSION" "service failed to start on v$VERSION — rolled back"
fi
log "service active on v$VERSION"

write_status ok "$VERSION"

# ---------- prune old releases (keep newest KEEP_RELEASES) ----------
ls -1dt "$RELEASES_DIR"/v* 2>/dev/null | tail -n +$((KEEP_RELEASES + 1)) | while read -r old; do
  # never delete the active target
  [ "$old" = "$(readlink "$CURRENT_LINK")" ] && continue
  log "pruning $old"
  rm -rf "$old"
done

log "update to v$VERSION complete"
