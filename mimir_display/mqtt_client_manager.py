"""
MQTT-based Display Client implementation (discovery mode only).

This module provides an MQTT-only display client that operates in discovery mode.
It is PURE-ASYNC: there is NO asyncio.run(...) here. Loop driving happens in the top-level entrypoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .bootstrap_manager import BootstrapManager
from .config import Config
from .content import DisplayManager, ImageCache
from .content.splash import generate_pair_code
from .hardware import get_display_capabilities
from .network import MDNSService, WebhookServer
from .network.mqtt import MqttDisplayClient
from .splash_renderer import SplashRenderer
from .utils import setup_logger
from .utils.helpers import resolve_writable_dir, sanitize_path
from .utils.orientation import parse_orientation


class MqttDisplayClientManager:
    """
    MQTT-based display client for discovery mode only.
    Thin orchestrator — wires together SplashRenderer, BootstrapManager,
    MqttDisplayClient, and supporting services.
    """

    def __init__(self, args=None):
        # Initialize configuration
        self.config = Config(args)

        # Initialize logging (must be before any logger usage)
        base_data_dir_raw = self.config.get("data_dir") or os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        base_data_dir_sanitized = sanitize_path(base_data_dir_raw)
        try:
            base_data_dir = resolve_writable_dir(base_data_dir_sanitized, "data_dir")
        except Exception:
            base_data_dir = resolve_writable_dir(None, "data_dir")
        self.log_dir = resolve_writable_dir(base_data_dir, "logs", subdir="logs")
        self.logger = setup_logger(self.log_dir, self.config.get("log_level", "INFO"))
        self._log_startup_marker(stage="boot")

        # Set up file system paths
        data_dir_raw = self.config.get("data_dir") or base_data_dir
        self.data_dir = resolve_writable_dir(sanitize_path(data_dir_raw), "data_dir")
        self.cache_dir = resolve_writable_dir(self.data_dir, "cache", subdir="cache")
        self.state_path = os.path.join(self.data_dir, "mqtt_state.json")

        # Normalize device id to hostname slug (always) to ensure topic stability
        raw_host = self.config.hostname
        original_id = self.config.display_id  # may be empty or user-provided
        def _slug(s: str) -> str:
            s = s.strip().lower().replace(" ", "-").replace("_", "-")
            s = re.sub(r"[^a-z0-9-]", "", s)
            s = re.sub(r"-+", "-", s)
            return s or "display"
        canonical_id = _slug(raw_host)
        if original_id and _slug(original_id) != canonical_id:
            self.logger.info("Overriding display_id '%s' with hostname canonical '%s'", original_id, canonical_id)
        self.config.set('display_id', canonical_id)

        # Load server-assigned config persisted from a previous pairing/finalize.
        from mimir_display.storage.device_config import DeviceConfig
        self.device_config = DeviceConfig()
        self._apply_device_config()

        # Get hardware capabilities after applying any persisted orientation override.
        self.capabilities = get_display_capabilities()
        self._log_startup_summary()

        # State management
        self.state: dict[str, Any] = {}
        self.stop_event = asyncio.Event()
        self.force_update_flag = False
        self.force_refresh_flag = False
        self._mqtt_config_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        # Load persistent state and apply any saved MQTT override
        self._load_state()
        self._apply_mqtt_override_from_state()

        # Generate pairing code (fixed for the lifetime of this session)
        self.pair_code: str = generate_pair_code()
        self.pair_code_published: bool = False

        # Splash renderer — owns all splash image state
        self.splash = SplashRenderer(
            config=self.config,
            data_dir=self.data_dir,
            pair_code=self.pair_code,
            logger=self.logger,
            get_capabilities=lambda: self.capabilities,
        )

        # Display the startup splash
        initial_status = "Searching for Mimir server..." if not self.config.platform_url else ""
        self.splash.render_startup(status_text=initial_status)

        # Optional startup test pattern for non e-ink framebuffer displays.
        if os.environ.get("STARTUP_TEST_PATTERN") == "1":
            backend_name = self.capabilities.get("backend", "")
            if backend_name == "hyperpixelsq":
                try:
                    from mimir_display.hardware.hyperpixelsq import display_test_pattern
                    display_test_pattern()
                    self.logger.info("Startup test pattern rendered (backend=%s)", backend_name)
                except Exception as e:  # noqa: BLE001
                    self.logger.debug("Startup test pattern failed: %s", e, exc_info=True)

        # Create metadata for registration
        from mimir_display.version import CLIENT_VERSION, PROTOCOL_VERSION

        self.metadata = {
            "name": self.config.display_name,
            "location": self.config.display_location,
            "hostname": self.config.hostname,
            # Real installed package version (matches the release tag), not a
            # config value — the server fleet panel and OTA rely on this.
            "client_version": CLIENT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "tags": self._split_tags(self.config.get("display_tags")),
        }

        # Initialize content management
        self.cache = ImageCache(self.cache_dir, self.logger)
        self.display_manager = DisplayManager(self.capabilities, self.cache_dir, self.logger)

        # Initialize MQTT client
        self.mqtt_client = MqttDisplayClient(
            self.config,
            self.capabilities,
            self.metadata,
            self._display_callback,
        )
        self.mqtt_client.set_pair_code(self.pair_code)
        self.mqtt_client.set_on_pair_status(self.splash.handle_pair_status)
        self.mqtt_client.set_on_first_connect(
            lambda: self.splash.update_status("Connected to MQTT — registering pair code…")
        )

        # Bootstrap manager — platform API fetch, broker setup, provision
        self.bootstrap = BootstrapManager(
            config=self.config,
            device_config=self.device_config,
            state=self.state,
            logger=self.logger,
            splash=self.splash,
            get_mqtt_client=lambda: self.mqtt_client,
            on_save_state=self._save_state,
            on_apply_orientation=self._apply_runtime_orientation,
            get_capabilities=lambda: self.capabilities,
            get_metadata=lambda: self.metadata,
        )

        # Initialize network services
        self.webhook_server = WebhookServer(self, self.config.webhook_port) if self.config.webhook_enabled else None
        self.provisioning_server = None
        self.mdns_service = MDNSService(self)

    # ------------------------------------------------------------------
    # Startup / logging helpers
    # ------------------------------------------------------------------

    def _log_startup_marker(self, *, stage: str) -> None:
        stage_label = (stage or "startup").strip().upper()
        border = "=" * 30
        self.logger.info("%s MIMIR DISPLAY %s %s", border, stage_label, border)

    def _log_startup_summary(self) -> None:
        resolution = self.capabilities.get("resolution") or ["?", "?"]
        backend = self.capabilities.get("backend") or "unknown"
        simulation = bool(self.capabilities.get("simulation_mode"))
        self.logger.info(
            "Startup summary hostname=%s display_id=%s backend=%s simulation=%s resolution=%sx%s platform_url=%s mqtt=%s:%s",
            self.config.hostname,
            self.config.display_id,
            backend,
            simulation,
            resolution[0],
            resolution[1],
            self.config.platform_url or "(unset)",
            self.config.mqtt_broker_host or "(unset)",
            self.config.mqtt_broker_port,
        )

    @staticmethod
    def _split_tags(tag_str: str | None) -> list[str]:
        if not tag_str:
            return []
        return [t.strip() for t in tag_str.split(",") if t.strip()]

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def display_id(self) -> str | None:
        """Get the current display ID (from config with hostname fallback)."""
        return self.config.display_id

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    async def _display_callback(self, content_path: Path, display_config: dict[str, Any]) -> dict[str, Any]:
        try:
            self.logger.info("Displaying content: %s", content_path)
            self.display_manager.display_from_file(str(content_path))

            # Persist scene assignment info if present
            scene_id = (display_config or {}).get("scene_id")
            subchannel_id = (display_config or {}).get("subchannel_id")
            if scene_id is not None:
                self.state["assigned_scene_id"] = scene_id
                self.state["scene_assigned_at"] = datetime.now(timezone.utc).isoformat()
            if subchannel_id is not None:
                self.state["assigned_subchannel_id"] = subchannel_id

            self.state["last_displayed"] = datetime.now(timezone.utc).isoformat()
            self.state["last_content_path"] = str(content_path)
            self.state["last_display_config"] = display_config
            self._save_state()

            try:
                fields = {}
                if scene_id is not None:
                    fields["scene_id"] = scene_id
                if subchannel_id is not None:
                    fields["subchannel_id"] = subchannel_id
                self.mqtt_client.presence.set_extra_fields(fields)
                await self.mqtt_client.presence.publish_status()
            except Exception as e:
                self.logger.debug("Could not republish status with scene_id/subchannel_id: %s", e)

            return {
                "displayed": True,
                "method": "display_manager",
                "config": display_config,
                "timestamp": self.state["last_displayed"],
            }

        except Exception as e:
            self.logger.error("Display callback failed: %s", e)
            self._display_default()
            return {
                "displayed": False,
                "error": str(e),
                "fallback": "default_content",
            }

    def _display_default(self):
        """Display default content."""
        try:
            default_path = self.config.get("default_content_path", "")
            if default_path and os.path.exists(default_path):
                self.display_manager.display_from_file(default_path)
                self.logger.info("Displayed default content")
            else:
                self.display_manager.display_default_content(default_path)
                self.logger.info("Displayed built-in default content")
        except Exception as e:
            self.logger.error("Failed to display default content: %s", e)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        """Load persistent state from disk."""
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    self.state = json.load(f)
                self.logger.debug("Loaded state from %s", self.state_path)
            except Exception as e:
                self.logger.warning("Failed to load state: %s", e)
                self.state = {}
        else:
            self.state = {}

    def _save_state(self):
        """Save persistent state to disk."""
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            self.logger.debug("Saved state to %s", self.state_path)
        except Exception as e:
            self.logger.error("Failed to save state: %s", e)

    def _apply_mqtt_override_from_state(self):
        override = self.state.get("mqtt_override")
        if not isinstance(override, dict):
            override = {}
        host = override.get("host")
        port = override.get("port")
        platform_url = self.state.get("platform_url_override")
        if host:
            self.config.set("mqtt_broker_host", host)
        if isinstance(port, int) and port > 0:
            self.config.set("mqtt_broker_port", port)
        if isinstance(platform_url, str) and platform_url:
            self.config.set("platform_url", platform_url)

    # ------------------------------------------------------------------
    # Config application
    # ------------------------------------------------------------------

    def _apply_device_config(self) -> None:
        """Apply server-assigned config from device_config.json.

        Only sets a value when the user has NOT explicitly configured it via
        .env — i.e. when the live config value is still the code default.
        .env always wins; this just fills in gaps for freshly-flashed devices.
        """
        dc = self.device_config
        if not dc.is_configured:
            return

        if dc.display_orientation:
            normalized_orientation = parse_orientation(dc.display_orientation)
            self.config.set("display_orientation", normalized_orientation)
            os.environ["DISPLAY_ORIENTATION"] = normalized_orientation
            self.logger.info("device_config: applied display_orientation=%s", normalized_orientation)

        candidates = [
            (dc.platform_url,     "PLATFORM_URL",       "platform_url",      ""),
            (dc.display_name,     "DISPLAY_NAME",       "display_name",      "Inky Display"),
            (dc.display_location, "DISPLAY_LOCATION",   "display_location",  "Unknown"),
            (dc.mqtt_host,        "MQTT_BROKER_HOST",   "mqtt_broker_host",  ""),
            (dc.mqtt_password,    "MQTT_PASSWORD",      "mqtt_password",     None),
            (dc.mqtt_username,    "MQTT_USERNAME",      "mqtt_username",     None),
        ]
        for stored_val, env_key, cfg_key, default in candidates:
            if stored_val is None:
                continue
            if os.environ.get(env_key):
                continue
            current = self.config.get(cfg_key)
            if current in (default, None, ""):
                self.config.set(cfg_key, stored_val)
                safe = "***" if "password" in cfg_key else stored_val
                self.logger.info("device_config: applied %s=%s", cfg_key, safe)

        if dc.mqtt_port and not os.environ.get("MQTT_BROKER_PORT"):
            if self.config.get("mqtt_broker_port") == 1883:
                self.config.set("mqtt_broker_port", dc.mqtt_port)
                self.logger.info("device_config: applied mqtt_broker_port=%d", dc.mqtt_port)

    async def _apply_runtime_orientation(self, orientation: str) -> bool:
        normalized = parse_orientation(orientation)
        current = parse_orientation(self.config.get("display_orientation") or os.environ.get("DISPLAY_ORIENTATION"))
        if normalized == current and self.capabilities.get("orientation") == normalized:
            return False

        self.config.set("display_orientation", normalized)
        os.environ["DISPLAY_ORIENTATION"] = normalized
        self.capabilities = get_display_capabilities()
        self.display_manager = DisplayManager(self.capabilities, self.cache_dir, self.logger)

        if getattr(self, "mqtt_client", None):
            await self.mqtt_client.refresh_runtime_capabilities(self.capabilities)
        if self.mdns_service and self.mdns_service.is_running():
            await self.mdns_service.update_properties()

        self.logger.info(
            "Runtime orientation updated to %s (%sx%s)",
            normalized,
            self.capabilities.get("resolution", ["?", "?"])[0],
            self.capabilities.get("resolution", ["?", "?"])[1],
        )
        return True

    # ------------------------------------------------------------------
    # Bootstrap config — thin wrapper for WebhookServer compatibility
    # ------------------------------------------------------------------

    def apply_bootstrap_config(self, payload: dict[str, Any]) -> None:
        """Called by WebhookServer thread. Delegates to BootstrapManager."""
        self.bootstrap.apply_bootstrap_config(payload)

    # ------------------------------------------------------------------
    # Network services
    # ------------------------------------------------------------------

    async def start_services(self):
        from .content.splash import get_local_ip
        from .network.provisioning_server import start_provisioning_server

        mdns_ok = False
        try:
            if self.mdns_service:
                mdns_ok = await self.mdns_service.start()
                self.logger.info("mDNS %s", "started" if mdns_ok else "not active")

            if self.webhook_server:
                self.webhook_server.start()
                self.logger.info("Webhook server started")

            if self.config.get("provisioning_enabled", True) and not self.provisioning_server:
                self.provisioning_server = start_provisioning_server(
                    hostname=self.config.hostname,
                    ip_address=get_local_ip(),
                    on_provisioned=self.bootstrap.apply_provision_bundle,
                    port=int(self.config.get("provisioning_port", 7777)),
                )
                self.logger.info(
                    "Provisioning server started on port %s",
                    self.config.get("provisioning_port", 7777),
                )

            if mdns_ok:
                print("Services started — display is discoverable via mDNS")
            else:
                print("Services started — mDNS not active; discovery via MQTT/webhook only")

        except Exception as e:
            self.logger.error("Failed to start services: %s", e, exc_info=True)

    async def stop_services(self):
        try:
            if self.mdns_service and self.mdns_service.is_running():
                await self.mdns_service.stop()
            if self.webhook_server:
                self.webhook_server.stop()
            if self.provisioning_server:
                self.provisioning_server.shutdown()
                self.provisioning_server.server_close()
                self.provisioning_server = None
        except Exception as e:
            self.logger.warning("Error stopping services: %s", e)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def discovery_mode_loop(self):
        """Run in discovery-only mode with MQTT presence."""
        self.logger.info("Starting discovery mode with MQTT presence")

        self._loop = asyncio.get_running_loop()
        self.bootstrap.set_loop(self._loop)

        await self.start_services()

        bootstrap_ready = await self.bootstrap.wait_for_bootstrap_config(self.stop_event)
        if not bootstrap_ready:
            self.logger.info("Stopping discovery mode before MQTT startup because bootstrap never completed")
            return

        print(f"Display ID: {self.display_id}")
        print(f"Display Name: {self.config.display_name}")
        print(f"Location: {self.config.display_location}")
        print(f"Hostname: {self.config.hostname}")
        print(f"Resolution: {self.capabilities['resolution']}")
        print(f"MQTT Broker: {self.config.mqtt_broker_host}:{self.config.mqtt_broker_port}")

        if self.config.get("mqtt_config_enabled", True):
            self._mqtt_config_task = asyncio.create_task(
                self.bootstrap.mqtt_config_poll_loop(self.stop_event), name="mqtt.config"
            )
        print("Waiting for API to discover and initiate registration...")
        print("Press Ctrl+C to stop...")

        listener = asyncio.create_task(self.mqtt_client.run_discovery_listener(), name="mqtt.discovery")

        try:
            done, pending = await asyncio.wait(
                {listener, asyncio.create_task(self.stop_event.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if self.stop_event.is_set() and not listener.done():
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener

            if listener in done:
                await listener

        except asyncio.CancelledError:
            if not listener.done():
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            raise
        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Webhook triggers and status
    # ------------------------------------------------------------------

    def trigger_update(self):
        """Trigger a manual update (called by webhook)."""
        self.force_update_flag = True
        self.logger.info("Manual update triggered")

    def trigger_refresh(self):
        """Trigger a manual refresh (called by webhook)."""
        self.force_refresh_flag = True
        self.logger.info("Manual refresh triggered")

    def get_status(self) -> dict[str, Any]:
        """Get current status information."""
        cache_info = self.mqtt_client.get_cache_info()
        registration_summary = self.mqtt_client.get_registration_summary()

        return {
            "display_id": self.display_id,
            "device_id": self.mqtt_client.device_id,
            "is_registered": self.mqtt_client.is_registered(),
            "mqtt_broker": f"{self.config.mqtt_broker_host}:{self.config.mqtt_broker_port}",
            "capabilities": self.capabilities,
            "metadata": self.metadata,
            "cache_info": cache_info,
            "registration": registration_summary,
            "state": self.state,
            "services": {
                "mdns": self.mdns_service.is_running() if self.mdns_service else False,
                "webhook": self.webhook_server.is_running() if self.webhook_server else False,
                "provisioning": self.provisioning_server is not None,
            },
        }

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self):
        """Shutdown the client gracefully (idempotent)."""
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True

        self.logger.info("Shutting down MQTT display client...")

        self.stop_event.set()

        await self.stop_services()

        if self._mqtt_config_task:
            self._mqtt_config_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._mqtt_config_task
            self._mqtt_config_task = None

        try:
            maybe = getattr(self.mqtt_client, "aclose", None) or getattr(self.mqtt_client, "disconnect", None)
            if callable(maybe):
                result = maybe()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
            self.logger.warning("Error closing MQTT client: %s", e)

        try:
            self.state["shutdown_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()
        except Exception as e:
            self.logger.warning("Error saving shutdown state: %s", e)

        self.logger.info("Shutdown complete")


# =========================
# Public async runner APIs
# =========================

async def run_mqtt_discovery_mode(args=None):
    """
    Async runner for discovery-only mode.
    This is intended to be awaited (or scheduled as a task) by the top-level entrypoint.
    """
    client = MqttDisplayClientManager(args)
    try:
        await client.discovery_mode_loop()
    finally:
        await client.shutdown()
