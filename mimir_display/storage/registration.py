"""Registration State Management

Handles persistence of device registration information for MQTT-based workflow.
Replaces the old registered.json format with MQTT-specific state tracking.

Enhancement (2025-09-23):
    The default storage location previously used ``Path.home() / ".mimir"`` which
    breaks under the hardening rules applied in the systemd unit (``ProtectHome=true``)
    causing ``PermissionError`` on startup when running as a restricted service user.

    We now resolve the state file path using the following precedence:
        1. Explicit ``state_file`` argument (mainly for tests)
        2. Environment variable ``MIMIR_STATE_DIR`` (recommended for packaged service)
        3. Writable ``/var/lib/mimir-display`` if it exists (default WorkingDirectory)
        4. Fallback to ``Path.home() / ".mimir"`` (developer local runs)

    The code gracefully falls back if a path is not writable and logs the decision.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from mimir_display.utils.helpers import resolve_writable_dir


class RegistrationState:
    """Manages persistent registration state for MQTT workflow.

    The state file path is dynamically resolved to support both developer and
    hardened service environments.
    """

    def __init__(self, state_file: Path = None):
        self.logger = logging.getLogger(__name__)
        # Resolve path with new precedence & fallbacks
        self.state_file = state_file or self._resolve_default_state_file()
        self._state: Dict[str, Any] = {}
        self._load_state()

    # ---------------------------------------------------------------------
    # Path resolution helpers
    # ---------------------------------------------------------------------
    def _resolve_default_state_file(self) -> Path:
        """Determine an appropriate location for the registration state file.

        Resolution behavior is intentionally compatibility-first:
        1. Reuse any existing registration_state.json from known legacy/current paths.
        2. If device_config.json already exists somewhere, store registration_state.json
           alongside it so the two persistence files stay together.
        3. Otherwise, fall back to the standard writable-dir resolver.
        """
        for candidate in self._candidate_state_files():
            if candidate.exists():
                self.logger.info("Using existing registration state file: %s", candidate)
                return candidate

        sibling_dir = self._detect_device_config_dir()
        if sibling_dir is not None:
            self.logger.info("Using registration state directory alongside device config: %s", sibling_dir)
            return sibling_dir / "registration_state.json"

        preferred = os.getenv("MIMIR_STATE_DIR")
        state_dir = resolve_writable_dir(preferred, "registration_state")
        self.logger.info("Using registration state directory: %s", state_dir)
        return Path(state_dir) / "registration_state.json"

    def _candidate_state_files(self) -> list[Path]:
        candidates: list[Path] = []
        preferred = os.getenv("MIMIR_STATE_DIR")
        if preferred:
            candidates.append(Path(preferred) / "registration_state.json")

        home = Path.home()
        xdg = os.getenv("XDG_DATA_HOME")
        if xdg:
            candidates.append(Path(xdg) / "mimir-display" / "registration_state.json")

        candidates.extend([
            Path("/var/lib/mimir-display/state/registration_state.json"),
            Path("/var/lib/mimir-display/registration_state.json"),
            home / ".mimir" / "registration_state.json",
            home / ".local" / "share" / "mimir-display" / "registration_state.json",
            Path("/tmp/mimir-display/registration_state.json"),
        ])

        seen: set[str] = set()
        ordered: list[Path] = []
        for candidate in candidates:
            key = str(candidate)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(candidate)
        return ordered

    def _detect_device_config_dir(self) -> Optional[Path]:
        candidates: list[Path] = []
        preferred = os.getenv("MIMIR_STATE_DIR")
        if preferred:
            candidates.append(Path(preferred))

        candidates.extend([
            Path("/var/lib/mimir-display/state"),
            Path("/var/lib/mimir-display"),
            Path.home() / ".mimir",
            Path.home() / ".local" / "share" / "mimir-display",
        ])

        for directory in candidates:
            if (directory / "device_config.json").exists():
                return directory
        return None
    
    def _load_state(self) -> None:
        """Load registration state from disk."""
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    self._state = json.load(f)
                self.logger.debug(f"Loaded registration state from {self.state_file}")
            except Exception as e:
                self.logger.warning(f"Failed to load registration state: {e}")
                self._state = {}
        else:
            self.logger.debug("No existing registration state found")
            self._state = {}
    
    def _save_state(self) -> None:
        """Save registration state to disk."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(self._state, f, indent=2)
            self.logger.debug(f"Saved registration state to {self.state_file}")
        except Exception as e:
            self.logger.error(f"Failed to save registration state: {e}")
    
    @property
    def is_registered(self) -> bool:
        """Check if device is currently registered."""
        return bool(self._state.get('assigned_id') and self._state.get('registration_timestamp'))
    
    @property
    def assigned_id(self) -> Optional[str]:
        """Get the assigned device ID from service."""
        return self._state.get('assigned_id')
    
    @property
    def device_id(self) -> Optional[str]:
        """Get the original device ID used for registration."""
        return self._state.get('device_id')
    
    @property
    def registration_timestamp(self) -> Optional[str]:
        """Get the registration timestamp."""
        return self._state.get('registration_timestamp')
    
    @property
    def service_config(self) -> Dict[str, Any]:
        """Get service-provided configuration."""
        return self._state.get('service_config', {})
    
    def update_registration(
        self,
        device_id: str,
        assigned_id: str,
        service_config: Dict[str, Any] = None
    ) -> None:
        """Update registration state with successful registration."""
        self._state.update({
            'device_id': device_id,
            'assigned_id': assigned_id,
            'service_config': service_config or {},
            'registration_timestamp': datetime.now(timezone.utc).isoformat(),
            'last_updated': datetime.now(timezone.utc).isoformat()
        })
        self._save_state()
        self.logger.info(f"Updated registration: {device_id} -> {assigned_id}")
    
    def clear_registration(self) -> None:
        """Clear registration state (device needs to re-register)."""
        old_assigned_id = self._state.get('assigned_id')
        self._state = {}
        self._save_state()
        self.logger.info(f"Cleared registration state for {old_assigned_id}")
    
    def update_heartbeat_config(self, heartbeat_interval: int) -> None:
        """Update heartbeat configuration from service."""
        if 'service_config' not in self._state:
            self._state['service_config'] = {}
        self._state['service_config']['heartbeat_interval'] = heartbeat_interval
        self._state['last_updated'] = datetime.now(timezone.utc).isoformat()
        self._save_state()
    
    def get_state_summary(self) -> Dict[str, Any]:
        """Get a summary of current registration state."""
        return {
            'is_registered': self.is_registered,
            'device_id': self.device_id,
            'assigned_id': self.assigned_id,
            'registration_age_hours': self._get_registration_age_hours(),
            'service_config': self.service_config
        }
    
    def _get_registration_age_hours(self) -> Optional[float]:
        """Get the age of registration in hours."""
        if not self.registration_timestamp:
            return None
        
        try:
            reg_time = datetime.fromisoformat(self.registration_timestamp.replace('Z', '+00:00'))
            now = datetime.now(timezone.utc)
            age = (now - reg_time).total_seconds() / 3600
            return round(age, 2)
        except Exception:
            return None
