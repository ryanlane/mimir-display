"""Client-side OTA update manager (Phase 3).

The display client never installs updates itself — it has no root. Instead it
evaluates the retained ``mimir/fleet/desired_version`` topic and, when an
update applies, writes a request file that the root-owned
``mimir-display-updater.path`` unit picks up (see scripts/ota_update.sh).

Flow:
    server (retained) -> handle_desired_version() -> request.json
        -> systemd path unit -> ota_update.sh (root): download, verify,
           A/B install, health check, symlink flip, restart, status.json

Because the topic is retained, every (re)connect re-delivers the current
desired version — displays that were offline catch up automatically, so no
separate polling loop is needed.

Files (OTA_DIR, default /var/lib/mimir-display/ota):
    request.json   written here (client), consumed by the updater
    status.json    written by the updater; surfaced in presence payloads
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mimir_display.version import CLIENT_VERSION

logger = logging.getLogger(__name__)

DEFAULT_OTA_DIR = "/var/lib/mimir-display/ota"
FAILED_RETRY_SECONDS = 60 * 60  # re-attempt a previously failed version hourly


def _norm(v: str | None) -> str:
    return str(v or "").lstrip("v").strip()


class OtaUpdateManager:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.ota_dir = Path(os.environ.get("OTA_DIR") or config.get("ota_dir", DEFAULT_OTA_DIR))

    # ---------- status (written by root updater) ----------
    def read_status(self) -> dict[str, Any]:
        path = self.ota_dir / "status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def presence_fields(self) -> dict[str, Any]:
        """Fields to merge into presence payloads (status + canary marker)."""
        fields: dict[str, Any] = {}
        if self._is_canary():
            fields["canary"] = True
        status = self.read_status()
        if status.get("result"):
            fields["update_status"] = status.get("result")  # ok | failed | in_progress
            if status.get("target_version"):
                fields["update_target"] = _norm(status.get("target_version"))
            if status.get("error"):
                fields["update_error"] = str(status.get("error"))[:200]
        return fields

    # ---------- desired-version evaluation ----------
    def _is_canary(self) -> bool:
        raw = self.config.get("display_tags") or ""
        tags = [t.strip().lower() for t in str(raw).replace(";", ",").split(",") if t.strip()]
        return "canary" in tags

    def _already_handled(self, target: str) -> str | None:
        """Return a skip-reason if this target version needs no action."""
        if _norm(target) == _norm(CLIENT_VERSION):
            return "already_running_target"
        req_path = self.ota_dir / "request.json"
        try:
            req = json.loads(req_path.read_text())
            if _norm(req.get("version")) == _norm(target):
                return "request_already_pending"
        except (OSError, json.JSONDecodeError):
            pass
        status = self.read_status()
        if (
            status.get("result") == "failed"
            and _norm(status.get("target_version")) == _norm(target)
        ):
            try:
                float(status.get("monotonic_hint", 0)) or 0
            except (TypeError, ValueError):
                pass
            # status.json carries wall-clock ts; use file mtime as retry clock
            try:
                age = time.time() - (self.ota_dir / "status.json").stat().st_mtime
            except OSError:
                age = FAILED_RETRY_SECONDS
            if age < FAILED_RETRY_SECONDS:
                return f"recent_failure_backoff({int(age)}s)"
        return None

    def handle_desired_version(self, payload: dict[str, Any]) -> bool:
        """Evaluate a desired_version payload; write an update request if it applies.

        Returns True when a request was written.
        """
        version = _norm(payload.get("version"))
        if not version:
            return False

        phase = str(payload.get("phase", "all")).lower()
        if phase == "canary" and not self._is_canary():
            logger.debug("OTA: desired v%s is canary-phase and this display is not a canary", version)
            return False

        skip = self._already_handled(version)
        if skip:
            logger.debug("OTA: skipping desired v%s (%s)", version, skip)
            return False

        download_path = payload.get("download_path")
        sha256 = payload.get("sha256")
        platform_url = (getattr(self.config, "platform_url", None) or self.config.get("platform_url") or "").rstrip("/")
        if not (download_path and sha256 and platform_url):
            logger.warning(
                "OTA: cannot act on desired v%s (download_path=%s sha256=%s platform_url=%s)",
                version, bool(download_path), bool(sha256), bool(platform_url),
            )
            return False

        request = {
            "version": version,
            "artifact": payload.get("artifact"),
            "download_url": f"{platform_url}{download_path}",
            "sha256": sha256,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "current_version": _norm(CLIENT_VERSION),
        }
        try:
            self.ota_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.ota_dir / "request.json.tmp"
            tmp.write_text(json.dumps(request, indent=2))
            tmp.replace(self.ota_dir / "request.json")
        except OSError as exc:
            logger.error("OTA: failed to write update request: %s", exc)
            return False

        logger.info(
            "OTA: update requested %s -> %s (phase=%s); root updater will take over",
            CLIENT_VERSION, version, phase,
        )
        return True
