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
from .network import WebhookServer, MDNSService, discover_mimir_server
from .network.provisioning_server import start_provisioning_server
from .content import ImageCache, DisplayManager
from .content.splash import build_splash, generate_pair_code, get_local_ip, overlay_status
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

        # Load server-assigned config persisted from a previous pairing/finalize.
        # Only overrides values the user has NOT explicitly set in .env (empty string
        # or default sentinel) so the operator can always override locally.
        from mimir_display.storage.device_config import DeviceConfig
        self.device_config = DeviceConfig()
        self._apply_device_config()

        # Generate a pairing code and store it for MQTT publishing after connection.
        # The code is generated locally so it can appear on the splash before MQTT starts.
        self.pair_code: str = generate_pair_code()
        self.pair_code_published: bool = False
        self._current_splash_status = ""

        # Build and display the dynamic startup splash screen:
        #   logo + QR code + 6-char pairing code + IP address
        self._splash_path: Optional[str] = None
        initial_status = "Searching for Mimir server..." if not self.config.platform_url else ""
        self._render_startup_splash(status_text=initial_status)

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
        # Pass the pairing code so it gets published on first MQTT connection
        self.mqtt_client.set_pair_code(self.pair_code)
        self.mqtt_client.set_on_pair_status(self._handle_pair_status)

        # MQTT connectivity alone is not enough; the pair code must be accepted
        # by the server before the user can successfully claim it.
        self.mqtt_client.set_on_first_connect(
            lambda: self._update_splash_status("Connected to MQTT — registering pair code…")
        )

        # Initialize network services
        self.webhook_server = WebhookServer(self, self.config.webhook_port) if self.config.webhook_enabled else None
        self.provisioning_server = None
        self.mdns_service = MDNSService(self)

        # State management
        self.state: Dict[str, Any] = {}
        self.stop_event = asyncio.Event()
        self.force_update_flag = False
        self.force_refresh_flag = False
        self._mqtt_config_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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


    def _update_splash_status(self, text: str, is_error: bool = False) -> None:
        """Overwrite the status banner on the startup splash and redisplay it."""
        if not self._splash_path or not os.path.exists(self._splash_path):
            return
        try:
            self._current_splash_status = text
            updated = overlay_status(self._splash_path, text, is_error=is_error)
            if updated is None:
                return
            updated.save(self._splash_path, format="PNG")
            dm = DisplayManager(self.capabilities, os.path.join(self.data_dir, "cache"), self.logger)
            dm.display_from_file(self._splash_path)
        except Exception as e:  # noqa: BLE001
            self.logger.debug("Failed to update splash status: %s", e)

    def _render_startup_splash(self, status_text: str = "") -> None:
        try:
            logo_path = (
                os.environ.get("STARTUP_LOGO_PATH")
                or os.path.join(os.path.dirname(__file__), "images", "startup.png")
            )
            res = self.capabilities.get("resolution") or [800, 480]
            splash_w, splash_h = int(res[0]), int(res[1])
            splash_img = build_splash(
                width=splash_w,
                height=splash_h,
                pair_code=self.pair_code,
                platform_url=self.config.platform_url or None,
                ip_address=get_local_ip(),
                logo_path=logo_path if os.path.exists(logo_path) else None,
                status_text=status_text,
            )

            splash_path = os.path.join(self.data_dir, "cache", "startup_splash.png")
            os.makedirs(os.path.dirname(splash_path), exist_ok=True)
            splash_img.save(splash_path, format="PNG")
            self._splash_path = splash_path
            self._current_splash_status = status_text

            tmp_dm = DisplayManager(self.capabilities, os.path.join(self.data_dir, "cache"), self.logger)
            tmp_dm.display_from_file(splash_path)
            self.logger.info(
                "Startup splash displayed (pair_code=%s, size=%dx%d, platform_url=%s)",
                self.pair_code,
                splash_w,
                splash_h,
                self.config.platform_url or "(unset)",
            )
        except Exception as e:  # noqa: BLE001 - non-fatal
            self.logger.debug("Startup splash failed: %s", e, exc_info=True)

    def _handle_pair_status(self, status: str, payload: Dict[str, Any]) -> None:
        """Reflect pair-code readiness on the splash screen."""
        if status in ("ok", "pending"):
            self._update_splash_status("Connected — enter code in Mimir to pair")
            return

        if status == "error":
            message = str(payload.get("message") or "Pairing setup failed")
            self._update_splash_status(message, is_error=True)

    def _apply_device_config(self) -> None:
        """Apply server-assigned config from device_config.json.

        Only sets a value when the user has NOT explicitly configured it via
        .env — i.e. when the live config value is still the code default.
        .env always wins; this just fills in gaps for freshly-flashed devices.
        """
        dc = self.device_config
        if not dc.is_configured:
            return

        # (value, env var name, config key, default sentinel)
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
            # If the operator set this env var explicitly, respect it
            if os.environ.get(env_key):
                continue
            # If current config is the default sentinel, override with stored value
            current = self.config.get(cfg_key)
            if current in (default, None, ""):
                self.config.set(cfg_key, stored_val)
                safe = "***" if "password" in cfg_key else stored_val
                self.logger.info("device_config: applied %s=%s", cfg_key, safe)

        # Port is an int — handle separately
        if dc.mqtt_port and not os.environ.get("MQTT_BROKER_PORT"):
            if self.config.get("mqtt_broker_port") == 1883:
                self.config.set("mqtt_broker_port", dc.mqtt_port)
                self.logger.info("device_config: applied mqtt_broker_port=%d", dc.mqtt_port)

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

    def _build_mqtt_config_url(self) -> str:
        explicit = self.config.get("mqtt_config_url")
        if explicit:
            return explicit
        base = (self.config.platform_url or "").rstrip("/")
        if not base:
            return ""
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
        platform_url = payload.get("platform_url")

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
        if isinstance(platform_url, str) and platform_url and platform_url != self.config.platform_url:
            self.config.set("platform_url", platform_url)
            changed = True

        if changed:
            self.state["mqtt_override"] = {
                "host": self.config.mqtt_broker_host,
                "port": self.config.mqtt_broker_port,
            }
            if platform_url:
                self.state["platform_url_override"] = self.config.platform_url
            self._save_state()
            self.logger.info(
                "MQTT broker updated via API: %s:%s",
                self.config.mqtt_broker_host,
                self.config.mqtt_broker_port,
            )
            self._render_startup_splash(status_text=self._current_splash_status)
            await self.mqtt_client.request_reconnect("api_config_update")
        return changed

    async def _apply_bootstrap_config_async(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        host = payload.get("host")
        port = payload.get("port")
        username = payload.get("username")
        password = payload.get("password")
        platform_url = payload.get("platform_url")
        reg_token = payload.get("reg_token")

        persisted = self.device_config.apply_bootstrap_payload(payload)

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
        if isinstance(platform_url, str) and platform_url and platform_url != self.config.platform_url:
            self.config.set("platform_url", platform_url)
            changed = True

        if reg_token:
            def _on_first_connect_provision() -> None:
                self._update_splash_status("Connected — self-registering…")
                task = asyncio.create_task(self._provision_self_register())
                task.add_done_callback(self._log_background_task_error)

            self.mqtt_client.set_on_first_connect(_on_first_connect_provision)

        if changed or persisted:
            self.state["mqtt_override"] = {
                "host": self.config.mqtt_broker_host,
                "port": self.config.mqtt_broker_port,
            }
            if platform_url:
                self.state["platform_url_override"] = self.config.platform_url
            self._save_state()
            self.logger.info(
                "Bootstrap config applied: mqtt=%s:%s reg_token=%s",
                self.config.mqtt_broker_host,
                self.config.mqtt_broker_port,
                "yes" if reg_token else "no",
            )
            self._render_startup_splash(status_text=self._current_splash_status)
            if changed:
                await self.mqtt_client.request_reconnect("bootstrap_config")

    def _log_background_task_error(self, task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc:
            self.logger.error("Background task failed: %s", exc, exc_info=exc)

    async def _provision_self_register(self) -> None:
        reg_token = self.device_config.reg_token
        platform_url = (self.config.platform_url or self.device_config.platform_url or "").rstrip("/")
        if not reg_token or not platform_url:
            self.logger.warning(
                "Provision self-register skipped: reg_token=%s platform_url=%s",
                "yes" if reg_token else "no",
                platform_url or "(unset)",
            )
            return

        endpoint = f"{platform_url}/api/displays/provision-register"
        payload = {
            "reg_token": reg_token,
            "device_id": self.mqtt_client.device_id,
            "hostname": self.config.hostname,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
        }

        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload) as resp:
                    body = await resp.text()
                    if resp.status >= 300:
                        self._update_splash_status("Provision register failed", is_error=True)
                        self.logger.error(
                            "Provision self-register failed status=%s body=%s",
                            resp.status,
                            body,
                        )
                        return
        except Exception as e:
            self._update_splash_status("Provision register failed", is_error=True)
            self.logger.error("Provision self-register error: %s", e, exc_info=True)
            return

        self._update_splash_status("Registered — waiting for finalize…")
        self.logger.info("Provision self-register succeeded via %s", endpoint)

    def _apply_provision_bundle(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            self.logger.warning("Ignoring invalid provision bundle payload")
            return

        mapped_payload = {
            "platform_url": payload.get("platform_url"),
            "host": payload.get("mqtt_host"),
            "port": payload.get("mqtt_port"),
            "username": payload.get("mqtt_username"),
            "password": payload.get("mqtt_password"),
            "reg_token": payload.get("reg_token"),
            "display_name": payload.get("display_name") or self.config.display_name,
            "display_location": payload.get("display_location") or self.config.display_location,
            "source": payload.get("source", "provision_bundle"),
        }
        self.logger.info(
            "Provision bundle received: mqtt=%s:%s reg_token=%s",
            mapped_payload.get("host") or "(unset)",
            mapped_payload.get("port") or "(unset)",
            "yes" if mapped_payload.get("reg_token") else "no",
        )
        self._update_splash_status("Provisioning received — applying…")
        self.apply_bootstrap_config(mapped_payload)

    def apply_bootstrap_config(self, payload: Dict[str, Any]) -> None:
        """Called by the webhook server thread to apply config."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._apply_bootstrap_config_async(payload), self._loop)
        else:
            self.logger.warning("Event loop not ready; bootstrap config ignored")

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

            if self.config.get("provisioning_enabled", True) and not self.provisioning_server:
                self.provisioning_server = start_provisioning_server(
                    hostname=self.config.hostname,
                    ip_address=get_local_ip(),
                    on_provisioned=self._apply_provision_bundle,
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

    def _has_valid_mqtt_host(self) -> bool:
        host = (self.config.mqtt_broker_host or "").strip()
        return host not in ("", "localhost", "127.0.0.1")

    async def _discover_platform_via_mdns(self) -> bool:
        self.logger.info("PLATFORM_URL not set — scanning mDNS for Mimir server (up to 10s)…")
        self._update_splash_status("Searching for Mimir server…")
        mdns_url = await discover_mimir_server(timeout=10.0)
        if not mdns_url:
            return False

        self.config.set("platform_url", mdns_url)
        self.logger.info("Mimir server discovered via mDNS: %s", mdns_url)
        self._render_startup_splash(status_text="Found Mimir server — fetching setup…")
        self.state["platform_url_override"] = mdns_url
        self._save_state()
        return True

    async def _wait_for_bootstrap_config(self) -> bool:
        while not self.stop_event.is_set():
            platform_url = (self.config.platform_url or "").strip()
            if not platform_url:
                discovered = await self._discover_platform_via_mdns()
                if not discovered:
                    self.logger.warning(
                        "No Mimir server found via mDNS. Waiting for manual PLATFORM_URL or bootstrap webhook."
                    )
                    self._update_splash_status(
                        "No Mimir server found — enable mDNS or set PLATFORM_URL",
                        is_error=True,
                    )
                    await asyncio.sleep(10)
                    continue

            bootstrap_ok = await self._refresh_mqtt_config(initial=True)
            if bootstrap_ok or self._has_valid_mqtt_host():
                return True

            self.logger.warning(
                "MQTT bootstrap config not available yet — waiting for server config. platform_url=%s mqtt_host=%s",
                self.config.platform_url or "(unset)",
                self.config.mqtt_broker_host or "(unset)",
            )
            self._update_splash_status(
                "Waiting for server setup — retrying bootstrap…",
                is_error=True,
            )
            await asyncio.sleep(10)

        return False

    async def discovery_mode_loop(self):
        """Run in discovery-only mode with MQTT presence."""
        self.logger.info("Starting discovery mode with MQTT presence")

        self._loop = asyncio.get_running_loop()

        # Start services (mDNS for discovery, webhook for manual triggers)
        await self.start_services()

        bootstrap_ready = await self._wait_for_bootstrap_config()
        if not bootstrap_ready:
            self.logger.info("Stopping discovery mode before MQTT startup because bootstrap never completed")
            return

        print(f"Display ID: {self.display_id}")
        print(f"Display Name: {self.config.display_name}")
        print(f"Location: {self.config.display_location}")
        print(f"Hostname: {self.config.hostname}")
        print(f"Resolution: {self.capabilities['resolution']}")
        print(f"MQTT Broker: {self.config.mqtt_broker_host}:{self.config.mqtt_broker_port}")

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
                "provisioning": self.provisioning_server is not None,
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
