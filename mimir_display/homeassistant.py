"""Home Assistant integration: the display as an HA device over MQTT discovery.

Enabled when ``HA_MQTT_HOST`` is set. It uses its own connection to Home
Assistant's broker (often not Mimir's), and publishes one retained
device-based discovery message describing:

* Photo (sensor; title from Mimir's image metadata, with artist, details,
  image URL and scene as attributes), Last image (timestamp), Channel
* Mimir connection (connectivity binary sensor)
* Orientation (select), New image / Redraw / Restart (buttons)
* Buttons A-D on Inky Impression boards (event entities: press, long_press)
* Diagnostics: CPU temperature, Wi-Fi signal, IP address, memory and disk
  use, last boot, undervoltage/throttling, client version, backend,
  resolution

Settings (environment / .env):

  HA_MQTT_HOST, HA_MQTT_PORT (1883), HA_MQTT_USERNAME, HA_MQTT_PASSWORD
  HA_DISCOVERY_PREFIX   default "homeassistant"
  HA_BUTTONS            false (default), true, or auto (on for Inky); off by
                        default because framed displays usually hide them
  HA_BUTTON_PINS        BCM pins for A,B,C,D (default 5,6,16,24; C is 25 on
                        the 13.3" Impression, picked automatically)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ORIENTATIONS = ["landscape", "portrait_left", "portrait_right"]
BUTTON_NAMES = ["a", "b", "c", "d"]
LONG_PRESS_S = 1.0
DEBOUNCE_S = 0.05
STATE_INTERVAL_S = 60


# --------------------------------------------------------------------------
# System readings (pure functions over /proc and /sys, testable with tmp files)
# --------------------------------------------------------------------------


def read_cpu_temp(path: str = "/sys/class/thermal/thermal_zone0/temp") -> float | None:
    try:
        return round(int(Path(path).read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def read_wifi_rssi(path: str = "/proc/net/wireless") -> int | None:
    """Signal level (dBm) of the first wireless interface, e.g. ``wlan0``."""
    try:
        for line in Path(path).read_text().splitlines()[2:]:
            parts = line.split()
            if len(parts) >= 4:
                return int(float(parts[3].rstrip(".")))
    except (OSError, ValueError):
        pass
    return None


def read_memory_used_percent(path: str = "/proc/meminfo") -> float | None:
    try:
        info = {}
        for line in Path(path).read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])
        total, avail = info["MemTotal"], info["MemAvailable"]
        return round(100.0 * (total - avail) / total, 1)
    except (OSError, ValueError, KeyError, ZeroDivisionError):
        return None


def read_boot_time(path: str = "/proc/uptime") -> str | None:
    try:
        uptime = float(Path(path).read_text().split()[0])
        boot = datetime.now(timezone.utc).timestamp() - uptime
        return datetime.fromtimestamp(boot, timezone.utc).replace(microsecond=0).isoformat()
    except (OSError, ValueError, IndexError):
        return None


def read_disk_used_percent(path: str = "/") -> float | None:
    try:
        u = shutil.disk_usage(path)
        return round(100.0 * u.used / u.total, 1)
    except OSError:
        return None


def parse_throttled(output: str) -> int | None:
    """``vcgencmd get_throttled`` -> bit field, e.g. "throttled=0x50005"."""
    m = re.search(r"0x([0-9a-fA-F]+)", output or "")
    return int(m.group(1), 16) if m else None


def read_throttled() -> int | None:
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
        return parse_throttled(out)
    except (OSError, subprocess.SubprocessError):
        return None


def local_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packets sent; picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return None


def caption_from_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Mimir's display_image metadata (e.g. mimir-source-metart) -> caption."""
    m = metadata or {}
    details = " · ".join(str(m[k]) for k in ("date", "medium", "department", "culture") if m.get(k))
    return {"title": str(m.get("title") or ""), "artist": str(m.get("artist") or ""), "details": details}


def default_button_pins(resolution: tuple[int, int] | list[int] | None) -> list[int]:
    # The 13.3" Impression (1600x1200) moved button C from GPIO 16 to 25.
    pins = [5, 6, 16, 24]
    if resolution and max(resolution) >= 1600:
        pins[2] = 25
    return pins


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def build_discovery(
    *,
    device_id: str,
    name: str,
    base: str,
    version: str,
    model: str,
    location: str | None = None,
    buttons: bool = False,
) -> dict[str, Any]:
    """One device-based discovery payload (HA 2024.11+) for every entity."""
    uid = re.sub(r"[^a-z0-9_]", "_", device_id.lower())
    dev: dict[str, Any] = {"ids": f"mimir_display_{uid}", "name": name, "mf": "Mimir", "mdl": model, "sw": version}
    if location and location.lower() != "unknown":
        dev["sa"] = location
    cmps: dict[str, Any] = {}

    def add(key: str, platform: str, ename: str | None, **extra: Any) -> None:
        cmps[key] = {"p": platform, "name": ename, "uniq_id": f"mimir_display_{uid}_{key}", **extra}

    state = f"{base}/state"
    diag = f"{base}/diag"
    add("photo", "sensor", "Photo", stat_t=state, val_tpl="{{ value_json.photo }}", ic="mdi:image",
        json_attr_t=f"{base}/photo")
    add("last_image", "sensor", "Last image", stat_t=state, val_tpl="{{ value_json.last_image }}",
        dev_cla="timestamp")
    add("channel", "sensor", "Channel", stat_t=state, val_tpl="{{ value_json.channel }}", ic="mdi:television-guide")
    add("mimir", "binary_sensor", "Mimir connection", stat_t=state,
        val_tpl="{{ 'ON' if value_json.mimir_connected else 'OFF' }}", dev_cla="connectivity")
    add("orientation", "select", "Orientation", stat_t=state, val_tpl="{{ value_json.orientation }}",
        cmd_t=f"{base}/orientation/set", options=ORIENTATIONS, ent_cat="config", ic="mdi:phone-rotate-landscape")
    add("new_image", "button", "New image", cmd_t=f"{base}/new_image/press", ic="mdi:image-refresh")
    add("redraw", "button", "Redraw", cmd_t=f"{base}/redraw/press", ic="mdi:monitor-shimmer")
    add("restart", "button", "Restart", cmd_t=f"{base}/restart/press", dev_cla="restart", ent_cat="config")
    for b in BUTTON_NAMES:
        if buttons:
            add(f"button_{b}", "event", f"Button {b.upper()}", stat_t=f"{base}/button/{b}",
                evt_typ=["press", "long_press"], dev_cla="button")
        else:
            cmps[f"button_{b}"] = {"p": "event"}  # platform only: removes it if it was there

    def sensor(key: str, ename: str, **extra: Any) -> None:
        add(key, "sensor", ename, stat_t=diag, val_tpl="{{ value_json." + key + " }}", ent_cat="diagnostic", **extra)

    sensor("cpu_temp", "CPU temperature", dev_cla="temperature", unit_of_meas="°C", stat_cla="measurement")
    sensor("wifi_rssi", "Wi-Fi signal", dev_cla="signal_strength", unit_of_meas="dBm", stat_cla="measurement")
    sensor("ip", "IP address", ic="mdi:ip-network")
    sensor("memory_used", "Memory used", unit_of_meas="%", stat_cla="measurement", ic="mdi:memory")
    sensor("disk_used", "Disk used", unit_of_meas="%", stat_cla="measurement", ic="mdi:harddisk")
    sensor("boot_time", "Last boot", dev_cla="timestamp")
    sensor("version", "Client version", ic="mdi:tag")
    sensor("backend", "Display backend", ic="mdi:monitor")
    sensor("resolution", "Resolution", ic="mdi:aspect-ratio")
    add("undervoltage", "binary_sensor", "Undervoltage", stat_t=diag,
        val_tpl="{{ 'ON' if value_json.undervoltage else 'OFF' }}", dev_cla="problem", ent_cat="diagnostic")
    add("throttled", "binary_sensor", "Throttled", stat_t=diag,
        val_tpl="{{ 'ON' if value_json.throttled else 'OFF' }}", dev_cla="problem", ent_cat="diagnostic")

    return {
        "dev": dev,
        "o": {"name": "mimir-display", "sw": version},
        "avty_t": f"{base}/availability",
        "cmps": cmps,
    }


# --------------------------------------------------------------------------
# Inky Impression buttons
# --------------------------------------------------------------------------


class ButtonWatcher:
    """Watches the Impression's buttons with libgpiod v2 (as the inky library
    does) on a thread, reporting "press" or "long_press" per button."""

    def __init__(self, pins: list[int], on_event: Callable[[str, str], None], logger: logging.Logger):
        self.pins = pins
        self.on_event = on_event
        self.logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            import gpiod  # type: ignore
            import gpiodevice  # type: ignore
            from gpiod.line import Bias, Direction, Edge  # type: ignore

            chip = gpiodevice.find_chip_by_platform()
            offsets = [chip.line_offset_from_id(p) for p in self.pins]
            settings = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP, edge_detection=Edge.BOTH)
            request = chip.request_lines(consumer="mimir-display-buttons", config=dict.fromkeys(offsets, settings))
        except Exception as e:  # noqa: BLE001 - optional hardware
            self.logger.info("HA: buttons not available (%s)", e)
            return False
        self._thread = threading.Thread(target=self._run, args=(request, offsets), name="ha.buttons", daemon=True)
        self._thread.start()
        self.logger.info("HA: watching buttons on GPIO %s", self.pins)
        return True

    def _run(self, request: Any, offsets: list[int]) -> None:
        from gpiod.edge_event import EdgeEvent  # type: ignore

        pressed_at: dict[int, float] = {}
        last_edge: dict[int, float] = {}
        while not self._stop.is_set():
            if not request.wait_edge_events(0.5):
                continue
            for ev in request.read_edge_events():
                now = time.monotonic()
                off = ev.line_offset
                if now - last_edge.get(off, 0) < DEBOUNCE_S:
                    continue
                last_edge[off] = now
                name = BUTTON_NAMES[offsets.index(off)]
                if ev.event_type == EdgeEvent.Type.FALLING_EDGE:  # pressed (pulled low)
                    pressed_at[off] = now
                elif off in pressed_at:
                    held = now - pressed_at.pop(off)
                    self.on_event(name, "long_press" if held >= LONG_PRESS_S else "press")

    def stop(self) -> None:
        self._stop.set()


# --------------------------------------------------------------------------
# The bridge
# --------------------------------------------------------------------------


class HomeAssistantBridge:
    """Owned by MqttDisplayClientManager; runs alongside the Mimir client."""

    def __init__(self, manager: Any, logger: logging.Logger | None = None):
        self.manager = manager
        self.logger = logger or logging.getLogger("mimir_display.homeassistant")
        self.host = os.getenv("HA_MQTT_HOST", "").strip()
        self.port = int(os.getenv("HA_MQTT_PORT", "1883") or 1883)
        self.username = os.getenv("HA_MQTT_USERNAME") or None
        self.password = os.getenv("HA_MQTT_PASSWORD") or None
        self.prefix = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant").strip() or "homeassistant"
        did = manager.display_id or manager.config.hostname
        self.device_id = re.sub(r"[^A-Za-z0-9_-]", "_", did)
        self.base = f"mimir_display/{self.device_id}"
        self.caption: dict[str, str] = {"title": "", "artist": "", "details": ""}
        self._client: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake = asyncio.Event()
        self._buttons: ButtonWatcher | None = None
        self._buttons_enabled = False
        self._last_state = ""
        self._mimir_record: dict[str, Any] = {}  # Mimir's last-image record, for scene/channel
        self._record_for = None  # last_displayed it was fetched for

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    # -- hooks from the client ------------------------------------------------

    def on_display_image(self, command: dict[str, Any]) -> None:
        """Called for every display_image command, before it's rendered."""
        meta = command.get("metadata")
        url = command.get("image_url") or command.get("url") or ""
        caption = caption_from_metadata(meta)
        # Mimir re-sends the current image without metadata on reconnect; keep
        # the description we have for the same URL.
        if not meta and url == self.manager.state.get("ha_caption_url"):
            caption = self.manager.state.get("ha_caption") or caption
        self.manager.state["ha_caption"] = caption
        self.manager.state["ha_caption_url"] = url
        self.caption = caption
        self.notify()

    def notify(self) -> None:
        """Something changed: publish state soon (thread-safe)."""
        if self._loop:
            self._loop.call_soon_threadsafe(self._wake.set)

    # -- state ----------------------------------------------------------------

    def _mimir_connected(self) -> bool:
        client = getattr(self.manager, "mqtt_client", None)
        last = getattr(client, "_last_mqtt_activity", None)
        interval = getattr(self.manager.config, "mqtt_heartbeat_interval", 30) or 30
        return last is not None and time.monotonic() - last < interval * 3

    def build_state(self) -> dict[str, Any]:
        st = self.manager.state
        caps = self.manager.capabilities or {}
        return {
            "photo": self.caption.get("title") or ("Untitled" if st.get("last_displayed") else "None"),
            "last_image": st.get("last_displayed"),
            "channel": (st.get("assigned_subchannel_id") or self._mimir_record.get("subchannel_id")
                        or st.get("assigned_scene_id") or self._mimir_record.get("scene_id")),
            "mimir_connected": self._mimir_connected(),
            "orientation": caps.get("orientation") or "landscape",
        }

    def build_photo_attrs(self) -> dict[str, Any]:
        st = self.manager.state
        return {
            "artist": self.caption.get("artist", ""),
            "details": self.caption.get("details", ""),
            "image_url": st.get("ha_caption_url", ""),
            "scene_id": st.get("assigned_scene_id") or self._mimir_record.get("scene_id"),
            "subchannel_id": st.get("assigned_subchannel_id") or self._mimir_record.get("subchannel_id"),
        }

    def build_diag(self) -> dict[str, Any]:
        caps = self.manager.capabilities or {}
        res = caps.get("resolution") or ["?", "?"]
        throttled = read_throttled()
        from mimir_display.version import CLIENT_VERSION

        return {
            "cpu_temp": read_cpu_temp(),
            "wifi_rssi": read_wifi_rssi(),
            "ip": local_ip(),
            "memory_used": read_memory_used_percent(),
            "disk_used": read_disk_used_percent(),
            "boot_time": read_boot_time(),
            "version": CLIENT_VERSION,
            "backend": caps.get("backend") or caps.get("display_type") or os.getenv("DISPLAY_BACKEND", "auto"),
            "resolution": f"{res[0]}x{res[1]}",
            # vcgencmd bits: 0 undervoltage now, 2 throttled now, 16/18 since boot
            "undervoltage": bool(throttled is not None and throttled & (1 << 0 | 1 << 16)),
            "throttled": bool(throttled is not None and throttled & (1 << 2 | 1 << 18)),
        }

    def _model(self) -> str:
        caps = self.manager.capabilities or {}
        backend = caps.get("backend") or os.getenv("DISPLAY_BACKEND", "")
        res = caps.get("native_resolution") or caps.get("resolution") or []
        size = f" {res[0]}x{res[1]}" if len(res) >= 2 else ""
        return f"Mimir display ({backend or 'auto'}{size})"

    def _want_buttons(self) -> bool:
        setting = os.getenv("HA_BUTTONS", "false").strip().lower()
        if setting in ("1", "true", "yes", "on"):
            return True
        if setting in ("0", "false", "no", "off"):
            return False
        caps = self.manager.capabilities or {}
        backend = (caps.get("backend") or os.getenv("DISPLAY_BACKEND", "")).lower()
        return "inky" in backend and not caps.get("simulation_mode")

    # -- actions --------------------------------------------------------------

    def platform_url(self) -> str:
        """Mimir's base URL, from the same places the rest of the client uses."""
        m = self.manager
        dc = getattr(m, "device_config", None)
        url = (m.config.platform_url or (dc.platform_url if dc is not None else None)
               or m.state.get("platform_url_override") or "")
        return url.rstrip("/")

    async def _scene_id(self, session: Any, platform: str) -> str | None:
        """The assigned scene: known locally after set_scene, otherwise from
        Mimir's record of the last image it sent this display."""
        scene = self.manager.state.get("assigned_scene_id") or getattr(
            self.manager.mqtt_client, "_assigned_scene_id", None)
        if scene:
            return scene
        device = self.manager.mqtt_client.device_id
        try:
            async with session.get(f"{platform}/api/displays/{device}/last-image") as r:
                if r.status == 200:
                    return (await r.json()).get("scene_id")
        except Exception as e:  # noqa: BLE001
            self.logger.debug("HA: last-image lookup failed: %s", e)
        return None

    async def request_new_image(self) -> bool:
        import aiohttp

        platform = self.platform_url()
        if not platform:
            self.logger.warning("HA: new image needs Mimir's address (PLATFORM_URL or the device config)")
            return False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as s:
                scene = await self._scene_id(s, platform)
                if not scene:
                    self.logger.warning("HA: new image needs a scene assigned in Mimir")
                    return False
                url = f"{platform}/api/scenes/{scene}/refresh"
                body = {"target_devices": [self.manager.mqtt_client.device_id], "reason": "home_assistant"}
                async with s.post(url, json=body) as r:
                    self.logger.info("HA: new image requested (HTTP %s)", r.status)
                    return 200 <= r.status < 300
        except Exception as e:  # noqa: BLE001
            self.logger.warning("HA: new image request failed: %s", e)
            return False

    async def redraw(self) -> None:
        path = self.manager.state.get("last_content_path")
        if not path or not os.path.exists(path):
            self.logger.info("HA: nothing to redraw")
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.manager.display_manager.display_from_file, path)

    async def set_orientation(self, value: str) -> None:
        if value not in ORIENTATIONS:
            return
        changed = await self.manager._apply_runtime_orientation(value)
        if not changed:
            return
        dc = getattr(self.manager, "device_config", None)
        if dc is not None and hasattr(dc, "_data"):
            dc._data["display_orientation"] = value  # survives restarts
            dc.save()
        await self.request_new_image()  # Mimir renders the new shape

    async def restart(self) -> None:
        self.logger.info("HA: restart requested")
        with contextlib.suppress(Exception):
            await self._client.publish(f"{self.base}/availability", "offline", retain=True)
        await asyncio.sleep(0.3)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    async def handle_command(self, topic: str, payload: str) -> None:
        leaf = topic[len(self.base) + 1 :]
        self.logger.info("HA: %s <- %s", leaf, payload)
        if leaf == "orientation/set":
            await self.set_orientation(payload.strip())
        elif leaf == "new_image/press":
            await self.request_new_image()
        elif leaf == "redraw/press":
            await self.redraw()
        elif leaf == "restart/press":
            await self.restart()
        self.notify()

    def _on_button(self, name: str, kind: str) -> None:
        self.logger.info("HA: button %s %s", name.upper(), kind)
        if self._loop and self._client:
            payload = json.dumps({"event_type": kind})
            asyncio.run_coroutine_threadsafe(self._client.publish(f"{self.base}/button/{name}", payload), self._loop)

    # -- run loop -------------------------------------------------------------

    async def _refresh_mimir_record(self) -> None:
        """After each new image, fetch Mimir's record of it for the scene and
        channel (the client only learns them from set_scene, which isn't
        always sent)."""
        last = self.manager.state.get("last_displayed")
        platform = self.platform_url()
        if not last or last == self._record_for or not platform:
            return
        self._record_for = last
        import aiohttp

        device = self.manager.mqtt_client.device_id
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(f"{platform}/api/displays/{device}/last-image") as r:
                    if r.status == 200:
                        self._mimir_record = await r.json()
        except Exception as e:  # noqa: BLE001
            self.logger.debug("HA: last-image lookup failed: %s", e)

    async def _publish_state(self, client: Any, force: bool) -> None:
        await self._refresh_mimir_record()
        state = json.dumps(self.build_state())
        if force or state != self._last_state:
            await client.publish(f"{self.base}/state", state, retain=True)
            await client.publish(f"{self.base}/photo", json.dumps(self.build_photo_attrs()), retain=True)
            self._last_state = state
        if force:
            loop = asyncio.get_running_loop()
            diag = await loop.run_in_executor(None, self.build_diag)
            await client.publish(f"{self.base}/diag", json.dumps(diag), retain=True)

    async def run(self, stop_event: asyncio.Event) -> None:
        import aiomqtt

        if not self.enabled:
            return
        self._loop = asyncio.get_running_loop()
        self.caption = self.manager.state.get("ha_caption") or self.caption
        self._buttons_enabled = self._want_buttons()
        if self._buttons_enabled:
            pins_env = os.getenv("HA_BUTTON_PINS", "").strip()
            caps = self.manager.capabilities or {}
            pins = [int(p) for p in pins_env.split(",")] if pins_env else default_button_pins(
                caps.get("native_resolution") or caps.get("resolution"))
            self._buttons = ButtonWatcher(pins, self._on_button, self.logger)
            self._buttons_enabled = self._buttons.start()

        will = aiomqtt.Will(f"{self.base}/availability", "offline", qos=1, retain=True)
        delay = 5
        while not stop_event.is_set():
            try:
                async with aiomqtt.Client(
                    self.host, self.port, username=self.username, password=self.password,
                    identifier=f"mimir-display-{self.device_id}-ha", will=will,
                ) as client:
                    self._client = client
                    delay = 5
                    discovery = build_discovery(
                        device_id=self.device_id, name=self.manager.config.display_name or self.device_id,
                        base=self.base, version=self._version(), model=self._model(),
                        location=self.manager.config.display_location, buttons=self._buttons_enabled,
                    )
                    await client.publish(f"{self.prefix}/device/mimir_display_{self.device_id}/config",
                                         json.dumps(discovery), retain=True)
                    await client.publish(f"{self.base}/availability", "online", retain=True)
                    await client.subscribe(f"{self.base}/+/set")
                    await client.subscribe(f"{self.base}/+/press")
                    self.logger.info("HA: connected to %s:%s as %s", self.host, self.port, self.device_id)
                    await self._serve(client, stop_event)
            except aiomqtt.MqttError as e:
                self.logger.warning("HA: MQTT error (%s); retrying in %ss", e, delay)
            finally:
                self._client = None
            if stop_event.is_set():
                break
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            delay = min(delay * 2, 120)
        if self._buttons:
            self._buttons.stop()

    async def _serve(self, client: Any, stop_event: asyncio.Event) -> None:
        async def commands() -> None:
            async for msg in client.messages:
                await self.handle_command(str(msg.topic), msg.payload.decode(errors="replace"))

        cmd_task = asyncio.create_task(commands(), name="ha.commands")
        last_full = 0.0
        try:
            while not stop_event.is_set():
                if cmd_task.done():
                    cmd_task.result()  # surfaces MqttError to reconnect
                force = time.monotonic() - last_full >= STATE_INTERVAL_S
                await self._publish_state(client, force)
                if force:
                    last_full = time.monotonic()
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=5)
        finally:
            cmd_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await cmd_task

    @staticmethod
    def _version() -> str:
        from mimir_display.version import CLIENT_VERSION

        return CLIENT_VERSION
