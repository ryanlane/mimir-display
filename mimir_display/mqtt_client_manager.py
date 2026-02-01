"""
MQTT-based Display Client implementation (discovery mode only).

This module provides an MQTT-only display client that operates in discovery mode.
It is PURE-ASYNC: there is NO asyncio.run(...) here. Loop driving happens in the top-level entrypoint.
"""

import asyncio
import contextlib
import json
import os
from typing import Any, Dict, Optional
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

from .config import Config
import re
from .network.mqtt_client import MqttDisplayClient
from .network import WebhookServer, MDNSService
from .content import ImageCache, DisplayManager
from .hardware import get_display_capabilities
from .utils import setup_logger
from .utils.helpers import resolve_writable_dir
from .utils.helpers import sanitize_path


class MqttDisplayClientManager:
    """
    MQTT-based display client for discovery mode only.
    Integrates MQTT communication, mDNS service, content management, and hardware abstraction.
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
            # Fall back to last-resort chain inside resolver with explicit None
            base_data_dir = resolve_writable_dir(None, "data_dir")
        self.log_dir = resolve_writable_dir(base_data_dir, "logs", subdir="logs")
        self.logger = setup_logger(self.log_dir, self.config.get("log_level", "INFO"))

        # Set up file system paths
        data_dir_raw = self.config.get("data_dir") or base_data_dir
        self.data_dir = resolve_writable_dir(sanitize_path(data_dir_raw), "data_dir")
        self.cache_dir = resolve_writable_dir(self.data_dir, "cache", subdir="cache")
        self.state_path = os.path.join(self.data_dir, "mqtt_state.json")

        # Normalize device id to hostname slug (always) to ensure topic stability
        raw_host = self.config.hostname
        original_id = self.config.display_id  # may be empty or user-provided
        # slug: lowercase, keep alnum and hyphen, replace spaces/underscores with '-'
        def _slug(s: str) -> str:
            s = s.strip().lower().replace(" ", "-").replace("_", "-")
            s = re.sub(r"[^a-z0-9-]", "", s)
            s = re.sub(r"-+", "-", s)
            return s or "display"
        canonical_id = _slug(raw_host)
        if original_id and _slug(original_id) != canonical_id:
            self.logger = setup_logger(self.log_dir, self.config.get("log_level", "INFO")) if not hasattr(self, 'logger') else self.logger
            self.logger.info("Overriding display_id '%s' with hostname canonical '%s'", original_id, canonical_id)
        # Force config display_id to canonical hostname-based slug
        self.config.set('display_id', canonical_id)

        # Get hardware capabilities (after stable id set)
        self.capabilities = get_display_capabilities()

        # Optional: display startup logo/image centered before any network activity.
        # This provides immediate visual feedback that the client launched.
        try:
            # Use provided image path (env override STARTUP_LOGO_PATH) or built-in resource.
            startup_logo = os.environ.get("STARTUP_LOGO_PATH") or os.path.join(os.path.dirname(__file__), "images", "startup.png")
            if os.path.exists(startup_logo):
                # Use a lightweight on-demand DisplayManager instance after capabilities retrieval.
                tmp_dm = DisplayManager(self.capabilities, os.path.join(self.data_dir, "cache"), self.logger)
                tmp_dm.display_from_file(startup_logo)
                self.logger.info("Startup logo displayed (%s)", startup_logo)
            else:
                self.logger.debug("No startup logo found at %s", startup_logo)
        except Exception as e:  # noqa: BLE001 - non-fatal
            self.logger.debug("Startup logo display failed: %s", e, exc_info=True)

        # Optional startup test pattern for non e-ink framebuffer displays.
        # Enabled when STARTUP_TEST_PATTERN=1 (default off). Only applies to color / non Inky backends.
        if os.environ.get("STARTUP_TEST_PATTERN") == "1":
            backend_name = self.capabilities.get("backend", "")
            if backend_name == "hyperpixelsq":
                try:
                    from mimir_display.hardware.hyperpixelsq import display_test_pattern  # lazy import
                    display_test_pattern()
                    self.logger.info("Startup test pattern rendered (backend=%s)", backend_name)
                except Exception as e:  # noqa: BLE001 - best-effort visual check
                    self.logger.debug("Startup test pattern failed: %s", e, exc_info=True)

        # Create metadata for registration
        self.metadata = {
            "name": self.config.display_name,
            "location": self.config.display_location,
            "hostname": self.config.hostname,
            "client_version": self.config.get("client_version", "1.0.0"),
            "tags": self._split_tags(self.config.get("display_tags")),
        }

        # Initialize content management
        self.cache = ImageCache(self.cache_dir, self.logger)
        self.display_manager = DisplayManager(self.capabilities, self.cache_dir, self.logger)

        # Initialize MQTT client with display callback
        self.mqtt_client = MqttDisplayClient(
            self.config,
            self.capabilities,
            self.metadata,
            self._display_callback,
        )

        # Initialize network services
        self.webhook_server = WebhookServer(self, self.config.webhook_port) if self.config.webhook_enabled else None
        self.mdns_service = MDNSService(self)

        # State management
        self.state: Dict[str, Any] = {}
        self.stop_event = asyncio.Event()
        self.force_update_flag = False
        self.force_refresh_flag = False
        self._mqtt_config_task: Optional[asyncio.Task] = None

        # Load persistent state
        self._load_state()

        # Apply persisted MQTT override (if any)
        self._apply_mqtt_override_from_state()

        # Setup signal handlers (best effort; may be ignored in some environments)
        # try:
        #     signal.signal(signal.SIGINT, self._signal_handler)
        #     signal.signal(signal.SIGTERM, self._signal_handler)
        # except Exception:
        #     # Some embedded/running envs won’t allow this; that’s fine.
        #     pass

    @staticmethod
    def _split_tags(tag_str: Optional[str]) -> list[str]:
        if not tag_str:
            return []
        return [t.strip() for t in tag_str.split(",") if t.strip()]

    @property
    def display_id(self) -> Optional[str]:
        """Get the current display ID (from config with hostname fallback)."""
        return self.config.display_id

    async def _display_callback(self, content_path: Path, display_config: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.logger.info("Displaying content: %s", content_path)

            # Render it
            self.display_manager.display_from_file(str(content_path))

            # Persist scene assignment info if present
            scene_id = (display_config or {}).get("scene_id")
            subchannel_id = (display_config or {}).get("subchannel_id")
            if scene_id is not None:
                self.state["assigned_scene_id"] = scene_id
                self.state["scene_assigned_at"] = datetime.now(timezone.utc).isoformat()
            if subchannel_id is not None:
                self.state["assigned_subchannel_id"] = subchannel_id

            # Existing state you already record
            self.state["last_displayed"] = datetime.now(timezone.utc).isoformat()
            self.state["last_content_path"] = str(content_path)
            self.state["last_display_config"] = display_config
            self._save_state()

            # Also push it into presence (see step 2)
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
                "timestamp": self.state["last_displayed"]
            }

        except Exception as e:
            self.logger.error("Display callback failed: %s", e)
            self._display_default()
            return {
                "displayed": False,
                "error": str(e),
                "fallback": "default_content"
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

    def _apply_mqtt_override_from_state(self):
        override = self.state.get("mqtt_override")
        if not isinstance(override, dict):
            return
        host = override.get("host")
        port = override.get("port")
        if host:
            self.config.set("mqtt_broker_host", host)
        if isinstance(port, int) and port > 0:
            self.config.set("mqtt_broker_port", port)

    def _build_mqtt_config_url(self) -> str:
        explicit = self.config.get("mqtt_config_url")
        if explicit:
            return explicit
        base = (self.config.platform_url or "").rstrip("/")
        endpoint = self.config.get("mqtt_config_endpoint", "/api/displays/mqtt/config")
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        return f"{base}{endpoint}"

    async def _fetch_mqtt_config(self) -> Optional[Dict[str, Any]]:
        if not self.config.get("mqtt_config_enabled", True):
            return None
        url = self._build_mqtt_config_url()
        if not url:
            return None
        headers = {}
        token = self.config.get("auth_token") or ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        self.logger.debug("MQTT config fetch failed status=%s", resp.status)
                        return None
                    return await resp.json()
        except Exception as e:  # noqa: BLE001
            self.logger.debug("MQTT config fetch error: %s", e)
            return None

    async def _refresh_mqtt_config(self, *, initial: bool = False) -> bool:
        payload = await self._fetch_mqtt_config()
        if not payload or not isinstance(payload, dict):
            return False

        if payload.get("enabled") is False:
            if initial:
                self.logger.info("MQTT config endpoint reports mqtt disabled")
            return False

        host = payload.get("host")
        port = payload.get("port")
        username = payload.get("username")
        password = payload.get("password")

        changed = False
        if isinstance(host, str) and host and host != self.config.mqtt_broker_host:
            self.config.set("mqtt_broker_host", host)
            changed = True
        if isinstance(port, int) and port > 0 and port != self.config.mqtt_broker_port:
            self.config.set("mqtt_broker_port", port)
            changed = True
        if username is not None and username != self.config.mqtt_username:
            self.config.set("mqtt_username", username)
            changed = True
        if password is not None and password != self.config.mqtt_password:
            self.config.set("mqtt_password", password)
            changed = True

        if changed:
            self.state["mqtt_override"] = {
                "host": self.config.mqtt_broker_host,
                "port": self.config.mqtt_broker_port,
            }
            self._save_state()
            self.logger.info(
                "MQTT broker updated via API: %s:%s",
                self.config.mqtt_broker_host,
                self.config.mqtt_broker_port,
            )
            await self.mqtt_client.request_reconnect("api_config_update")
        return changed

    async def _mqtt_config_poll_loop(self):
        interval = max(10, int(self.config.get("mqtt_config_poll_seconds", 60)))
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            await self._refresh_mqtt_config()

    def _save_state(self):
        """Save persistent state to disk."""
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            self.logger.debug("Saved state to %s", self.state_path)
        except Exception as e:
            self.logger.error("Failed to save state: %s", e)

    # def _signal_handler(self, signum, frame):
    #     """Handle shutdown signals."""
    #     self.logger.info(f"Received signal {signum}")
    #     # Schedule shutdown on the running loop
    #     try:
    #         loop = asyncio.get_running_loop()
    #         loop.create_task(self.shutdown())
    #     except RuntimeError:
    #         # No loop; ignore (top-level will handle)
    #         pass

    async def start_services(self):
        mdns_ok = False
        try:
            if self.mdns_service:
                mdns_ok = await self.mdns_service.start()
                self.logger.info("mDNS %s", "started" if mdns_ok else "not active")

            if self.webhook_server:
                self.webhook_server.start()
                self.logger.info("Webhook server started")

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
        except Exception as e:
            self.logger.warning("Error stopping services: %s", e)

    async def discovery_mode_loop(self):
        """Run in discovery-only mode with MQTT presence."""
        self.logger.info("Starting discovery mode with MQTT presence")

        await self._refresh_mqtt_config(initial=True)

        print(f"Display ID: {self.display_id}")
        print(f"Display Name: {self.config.display_name}")
        print(f"Location: {self.config.display_location}")
        print(f"Hostname: {self.config.hostname}")
        print(f"Resolution: {self.capabilities['resolution']}")
        print(f"MQTT Broker: {self.config.mqtt_broker_host}:{self.config.mqtt_broker_port}")

        # Start services (mDNS for discovery, webhook for manual triggers)
        await self.start_services()

        # Start MQTT config polling (optional)
        if self.config.get("mqtt_config_enabled", True):
            self._mqtt_config_task = asyncio.create_task(self._mqtt_config_poll_loop(), name="mqtt.config")
        print("Waiting for API to discover and initiate registration...")
        print("Press Ctrl+C to stop...")

        # Run the long-lived listener as a task
        listener = asyncio.create_task(self.mqtt_client.run_discovery_listener(), name="mqtt.discovery")

        try:
            # Wait until either the listener ends or stop_event is set
            done, pending = await asyncio.wait(
                {listener, asyncio.create_task(self.stop_event.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # If stop_event fired, cancel the listener
            if self.stop_event.is_set() and not listener.done():
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener

            # If listener finished first, propagate errors if any
            if listener in done:
                # will raise if the task crashed
                await listener

        except asyncio.CancelledError:
            # External cancel (e.g., Ctrl+C from entrypoint) -> cancel listener then exit
            if not listener.done():
                listener.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await listener
            raise
        finally:
            await self.shutdown()

    def trigger_update(self):
        """Trigger a manual update (called by webhook)."""
        self.force_update_flag = True
        self.logger.info("Manual update triggered")

    def trigger_refresh(self):
        """Trigger a manual refresh (called by webhook)."""
        self.force_refresh_flag = True
        self.logger.info("Manual refresh triggered")

    def get_status(self) -> Dict[str, Any]:
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
            },
        }

    async def shutdown(self):
        """Shutdown the client gracefully (idempotent)."""
        if getattr(self, "_shutting_down", False):
            return
        self._shutting_down = True

        self.logger.info("Shutting down MQTT display client...")

        # Signal stop
        self.stop_event.set()

        # Stop services (sync)
        await self.stop_services()

        if self._mqtt_config_task:
            self._mqtt_config_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._mqtt_config_task
            self._mqtt_config_task = None

        # Close MQTT client if it supports an async close/disconnect
        try:
            maybe = getattr(self.mqtt_client, "aclose", None) or getattr(self.mqtt_client, "disconnect", None)
            if callable(maybe):
                result = maybe()
                if asyncio.iscoroutine(result):
                    await result
        except Exception as e:
            self.logger.warning(f"Error closing MQTT client: {e}")

        # Save final state
        try:
            self.state["shutdown_at"] = datetime.now(timezone.utc).isoformat()
            self._save_state()
        except Exception as e:
            self.logger.warning(f"Error saving shutdown state: {e}")

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
        # Ensure cleanup if discovery loop exits for any reason
        await client.shutdown()
