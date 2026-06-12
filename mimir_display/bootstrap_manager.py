from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import aiohttp

from .network import discover_mimir_server


class BootstrapManager:
    """Handles platform API config fetch, MQTT broker setup, and retry/polling.

    Responsible for:
    - Fetching MQTT broker config from the platform API
    - Applying bootstrap/provision payloads (host, port, credentials, orientation)
    - Running the mDNS discovery loop until a Mimir server is found
    - Polling for config changes during the session

    Dependencies are injected at construction time. The event loop is set separately
    via set_loop() once the async context is entered.
    """

    def __init__(
        self,
        *,
        config,
        device_config,
        state: dict[str, Any],
        logger: logging.Logger,
        splash,                                           # SplashRenderer
        get_mqtt_client: Callable[[], Any],               # lazy — client built after __init__
        on_save_state: Callable[[], None],
        on_apply_orientation: Callable[[str], Awaitable[bool]],
        get_capabilities: Callable[[], dict[str, Any]],
        get_metadata: Callable[[], dict[str, Any]],
    ) -> None:
        self.config = config
        self.device_config = device_config
        self.state = state  # shared mutable dict — mutations visible to orchestrator
        self.logger = logger
        self.splash = splash
        self._get_mqtt_client = get_mqtt_client
        self._on_save_state = on_save_state
        self._on_apply_orientation = on_apply_orientation
        self._get_capabilities = get_capabilities
        self._get_metadata = get_metadata
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the running event loop for thread-safe scheduling."""
        self._loop = loop

    # ------------------------------------------------------------------
    # Public API — called from orchestrator or external threads
    # ------------------------------------------------------------------

    def apply_bootstrap_config(self, payload: dict[str, Any]) -> None:
        """Thread-safe wrapper — called by WebhookServer thread."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._apply_bootstrap_config_async(payload), self._loop
            )
        else:
            self.logger.warning("Event loop not ready; bootstrap config ignored")

    def apply_provision_bundle(self, payload: dict[str, Any]) -> None:
        """Called by the provisioning server's on_provisioned callback."""
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
        self.splash.update_status("Provisioning received — applying…")
        self.apply_bootstrap_config(mapped_payload)

    async def wait_for_bootstrap_config(self, stop_event: asyncio.Event) -> bool:
        """Block until a valid MQTT host is known or stop_event is set."""
        while not stop_event.is_set():
            platform_url = (self.config.platform_url or "").strip()
            if not platform_url:
                discovered = await self._discover_platform_via_mdns()
                if not discovered:
                    self.logger.warning(
                        "No Mimir server found via mDNS. Waiting for manual PLATFORM_URL or bootstrap webhook."
                    )
                    self.splash.update_status(
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
            self.splash.update_status(
                "Waiting for server setup — retrying bootstrap…",
                is_error=True,
            )
            await asyncio.sleep(10)

        return False

    async def mqtt_config_poll_loop(self, stop_event: asyncio.Event) -> None:
        """Periodically re-fetch broker config during the session."""
        interval = max(10, int(self.config.get("mqtt_config_poll_seconds", 60)))
        while not stop_event.is_set():
            await asyncio.sleep(interval)
            await self._refresh_mqtt_config()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_valid_mqtt_host(self) -> bool:
        host = (self.config.mqtt_broker_host or "").strip()
        return host not in ("", "localhost", "127.0.0.1")

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

    async def _fetch_mqtt_config(self) -> dict[str, Any] | None:
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
            self._on_save_state()
            self.logger.info(
                "MQTT broker updated via API: %s:%s",
                self.config.mqtt_broker_host,
                self.config.mqtt_broker_port,
            )
            self.splash.render_startup(status_text=self.splash.current_status)
            await self._get_mqtt_client().request_reconnect("api_config_update")
        return changed

    async def _apply_bootstrap_config_async(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        host = payload.get("host")
        port = payload.get("port")
        username = payload.get("username")
        password = payload.get("password")
        platform_url = payload.get("platform_url")
        reg_token = payload.get("reg_token")
        display_orientation = payload.get("display_orientation")

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
        orientation_changed = False
        if isinstance(display_orientation, str) and display_orientation.strip():
            orientation_changed = await self._on_apply_orientation(display_orientation)

        if reg_token:
            mqtt_client = self._get_mqtt_client()

            def _start_self_register() -> None:
                self.logger.info(
                    "Provision self-register starting device_id=%s endpoint=%s/api/displays/provision-register",
                    mqtt_client.device_id,
                    (self.config.platform_url or self.device_config.platform_url or "").rstrip("/"),
                )
                self.splash.update_status("Connected — self-registering…")
                task = asyncio.create_task(self._provision_self_register())
                task.add_done_callback(self._log_background_task_error)

            def _on_first_connect_provision() -> None:
                _start_self_register()

            if getattr(mqtt_client, "_client", None) is not None:
                self.logger.info(
                    "Bootstrap config received after MQTT connect; triggering immediate self-register"
                )
                _start_self_register()
            else:
                mqtt_client.set_on_first_connect(_on_first_connect_provision)

        if changed or persisted or orientation_changed:
            self.state["mqtt_override"] = {
                "host": self.config.mqtt_broker_host,
                "port": self.config.mqtt_broker_port,
            }
            if platform_url:
                self.state["platform_url_override"] = self.config.platform_url
            self._on_save_state()
            self.logger.info(
                "Bootstrap config applied: mqtt=%s:%s reg_token=%s orientation=%s",
                self.config.mqtt_broker_host,
                self.config.mqtt_broker_port,
                "yes" if reg_token else "no",
                self._get_capabilities().get("orientation"),
            )
            mqtt_client = self._get_mqtt_client()
            if getattr(mqtt_client, "_client", None) is None:
                self.splash.render_startup(status_text=self.splash.current_status)
            else:
                self.logger.debug("Skipping startup splash redraw during active MQTT session")
            if changed:
                await mqtt_client.request_reconnect("bootstrap_config")

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

        mqtt_client = self._get_mqtt_client()
        endpoint = f"{platform_url}/api/displays/provision-register"
        payload = {
            "reg_token": reg_token,
            "device_id": mqtt_client.device_id,
            "hostname": self.config.hostname,
            "capabilities": self._get_capabilities(),
            "metadata": self._get_metadata(),
        }

        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload) as resp:
                    body = await resp.text()
                    if resp.status >= 300:
                        self.splash.update_status("Provision register failed", is_error=True)
                        self.logger.error(
                            "Provision self-register failed status=%s body=%s",
                            resp.status,
                            body,
                        )
                        return
        except Exception as e:
            self.splash.update_status("Provision register failed", is_error=True)
            self.logger.error("Provision self-register error: %s", e, exc_info=True)
            return

        self.splash.update_status("Registered — waiting for finalize…")
        self.logger.info("Provision self-register succeeded via %s", endpoint)

    async def _discover_platform_via_mdns(self) -> bool:
        self.logger.info("PLATFORM_URL not set — scanning mDNS for Mimir server (up to 10s)…")
        self.splash.update_status("Searching for Mimir server…")
        mdns_url = await discover_mimir_server(timeout=10.0)
        if not mdns_url:
            return False

        self.config.set("platform_url", mdns_url)
        self.logger.info("Mimir server discovered via mDNS: %s", mdns_url)
        self.splash.render_startup(status_text="Found Mimir server — fetching setup…")
        self.state["platform_url_override"] = mdns_url
        self._on_save_state()
        return True
