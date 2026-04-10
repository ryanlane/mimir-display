"""
Persistent device configuration.

Stores server-assigned configuration so it survives reboots without needing
manual .env edits.  Written by _handle_finalize_registration when the server
sends pairing confirmation; read at startup before the MQTT connection is made.

File location follows the same precedence as RegistrationState:
  1. $MIMIR_STATE_DIR / device_config.json
  2. /var/lib/mimir-display / device_config.json
  3. ~/.mimir / device_config.json

Priority of config values (highest first):
  .env file  >  device_config.json  >  code defaults

This file only stores values pushed by the server.  .env always wins so the
operator can override anything locally without fighting the auto-config.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)

_FILENAME = "device_config.json"
_CANDIDATE_DIRS = [
    Path(os.environ.get("MIMIR_STATE_DIR", "") or "/nonexistent"),
    Path("/var/lib/mimir-display"),
    Path.home() / ".mimir",
]


def _resolve_path() -> Path:
    for d in _CANDIDATE_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
            test = d / ".write_test"
            test.write_text("ok")
            test.unlink(missing_ok=True)
            return d / _FILENAME
        except (PermissionError, OSError):
            continue
    fallback = Path.cwd() / _FILENAME
    logger.warning("All state dirs unwritable; using %s", fallback)
    return fallback


class DeviceConfig:
    """Load / save server-assigned device configuration."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or _resolve_path()
        self._data: Dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                logger.debug("Loaded device config from %s", self._path)
            except Exception as exc:
                logger.warning("Failed to load device config: %s", exc)
                self._data = {}

    def save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, indent=2), encoding="utf-8"
            )
            logger.debug("Saved device config to %s", self._path)
        except Exception as exc:
            logger.error("Failed to save device config: %s", exc)

    # ------------------------------------------------------------------ update

    def apply_finalize_payload(self, payload: Dict[str, Any]) -> None:
        """Merge the config block from a finalize_registration command."""
        cfg: Dict[str, Any] = payload.get("config") or {}
        if not cfg:
            return

        mapping = {
            "platform_url":     "platform_url",
            "display_name":     "display_name",
            "display_location": "display_location",
            "display_orientation": "display_orientation",
            "mqtt_host":        "mqtt_host",
            "mqtt_port":        "mqtt_port",
            "mqtt_username":    "mqtt_username",
            "mqtt_password":    "mqtt_password",
            "reg_token":        "reg_token",
        }
        changed = False
        for src, dst in mapping.items():
            val = cfg.get(src)
            if val is not None:
                self._data[dst] = val
                changed = True

        if changed:
            self._data["configured_at"] = datetime.now(timezone.utc).isoformat()
            self._data["configured_by"] = payload.get("source", "pairing_code")
            self.save()
            logger.info(
                "Device config updated from server: %s",
                {k: v for k, v in self._data.items() if "password" not in k},
            )

    def apply_bootstrap_payload(self, payload: Dict[str, Any]) -> bool:
        """Persist a webhook/bootstrap config payload using the same config keys."""
        if not isinstance(payload, dict) or not payload:
            return False

        mapping = {
            "platform_url": "platform_url",
            "display_name": "display_name",
            "display_location": "display_location",
            "display_orientation": "display_orientation",
            "host": "mqtt_host",
            "port": "mqtt_port",
            "username": "mqtt_username",
            "password": "mqtt_password",
            "reg_token": "reg_token",
        }
        changed = False
        for src, dst in mapping.items():
            val = payload.get(src)
            if val is not None and self._data.get(dst) != val:
                self._data[dst] = val
                changed = True

        if changed:
            self._data["configured_at"] = datetime.now(timezone.utc).isoformat()
            self._data["configured_by"] = payload.get("source", "bootstrap")
            self.save()
        return changed

    # ------------------------------------------------------------------ read

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def platform_url(self) -> Optional[str]:
        return self._data.get("platform_url")

    @property
    def display_name(self) -> Optional[str]:
        return self._data.get("display_name")

    @property
    def display_location(self) -> Optional[str]:
        return self._data.get("display_location")

    @property
    def display_orientation(self) -> Optional[str]:
        return self._data.get("display_orientation")

    @property
    def mqtt_host(self) -> Optional[str]:
        return self._data.get("mqtt_host")

    @property
    def mqtt_port(self) -> Optional[int]:
        v = self._data.get("mqtt_port")
        return int(v) if v is not None else None

    @property
    def mqtt_username(self) -> Optional[str]:
        return self._data.get("mqtt_username")

    @property
    def mqtt_password(self) -> Optional[str]:
        return self._data.get("mqtt_password")

    @property
    def reg_token(self) -> Optional[str]:
        return self._data.get("reg_token")

    @property
    def is_configured(self) -> bool:
        return bool(self._data.get("configured_at"))
