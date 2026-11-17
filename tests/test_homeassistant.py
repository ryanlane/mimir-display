"""Unit tests for the Home Assistant bridge (no broker or hardware needed)."""
import asyncio
import time

import pytest

import mimir_display.homeassistant as ha


class FakeConfig:
    hostname = "colorframe05"
    display_name = "Landscape Dev 01"
    display_location = "Workbench"
    platform_url = "http://oak:8080"
    mqtt_heartbeat_interval = 30


class FakeMqttClient:
    device_id = "colorframe05"

    def __init__(self):
        self._last_mqtt_activity = time.monotonic()


class FakeManager:
    def __init__(self):
        self.config = FakeConfig()
        self.display_id = "colorframe05"
        self.state = {}
        self.capabilities = {
            "backend": "inky",
            "resolution": [800, 480],
            "native_resolution": [800, 480],
            "orientation": "landscape",
            "simulation_mode": False,
        }
        self.mqtt_client = FakeMqttClient()
        self.orientations = []

    async def _apply_runtime_orientation(self, value):
        self.orientations.append(value)
        self.capabilities["orientation"] = value
        return True


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("HA_MQTT_HOST", "192.168.1.18")
    return ha.HomeAssistantBridge(FakeManager())


def test_disabled_without_host(monkeypatch):
    monkeypatch.delenv("HA_MQTT_HOST", raising=False)
    assert not ha.HomeAssistantBridge(FakeManager()).enabled


def test_discovery_payload_shape():
    d = ha.build_discovery(device_id="colorframe05", name="Dev", base="mimir_display/colorframe05",
                           version="1.0.53", model="m", location="Workbench", buttons=True)
    assert d["dev"]["ids"] == "mimir_display_colorframe05"
    assert d["dev"]["sa"] == "Workbench"
    assert d["avty_t"] == "mimir_display/colorframe05/availability"
    cmps = d["cmps"]
    assert {"photo", "orientation", "new_image", "redraw", "restart", "cpu_temp", "undervoltage"} <= set(cmps)
    assert {f"button_{b}" for b in "abcd"} <= set(cmps)
    assert cmps["orientation"]["options"] == ha.ORIENTATIONS
    assert cmps["button_a"]["p"] == "event" and cmps["button_a"]["evt_typ"] == ["press", "long_press"]
    uids = [c["uniq_id"] for c in cmps.values()]
    assert len(uids) == len(set(uids))


def test_discovery_without_buttons_or_location():
    d = ha.build_discovery(device_id="Frame-1", name="F", base="b", version="1", model="m", location="Unknown")
    assert d["cmps"]["button_a"] == {"p": "event"}  # removal stubs only
    assert "sa" not in d["dev"]
    assert d["dev"]["ids"] == "mimir_display_frame_1"


def test_readings(tmp_path):
    (tmp_path / "temp").write_text("48312\n")
    assert ha.read_cpu_temp(str(tmp_path / "temp")) == 48.3
    (tmp_path / "wireless").write_text(
        "Inter-| sta-|   Quality        |   Discarded packets\n"
        " face | tus | link level noise |  nwid  crypt   frag\n"
        " wlan0: 0000   58.  -52.  -256        0      0      0\n")
    assert ha.read_wifi_rssi(str(tmp_path / "wireless")) == -52
    (tmp_path / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 100 kB\nMemAvailable: 750 kB\n")
    assert ha.read_memory_used_percent(str(tmp_path / "meminfo")) == 25.0
    (tmp_path / "uptime").write_text("100.0 50.0\n")
    assert ha.read_boot_time(str(tmp_path / "uptime"))
    assert ha.read_cpu_temp(str(tmp_path / "missing")) is None
    assert ha.parse_throttled("throttled=0x50005") == 0x50005
    assert ha.parse_throttled("") is None


def test_caption_from_metadata():
    c = ha.caption_from_metadata({"title": "Ptilota plumosa. ß capillaris", "artist": "Anna Atkins",
                                  "date": "ca. 1853", "medium": "Cyanotype", "department": "Photographs"})
    assert c == {"title": "Ptilota plumosa. ß capillaris", "artist": "Anna Atkins",
                 "details": "ca. 1853 · Cyanotype · Photographs"}
    assert ha.caption_from_metadata(None) == {"title": "", "artist": "", "details": ""}


def test_button_pins():
    assert ha.default_button_pins([800, 480]) == [5, 6, 16, 24]
    assert ha.default_button_pins([1600, 1200]) == [5, 6, 25, 24]


def test_caption_kept_when_mimir_resends_without_metadata(bridge):
    bridge.on_display_image({"image_url": "http://x/a.jpg", "metadata": {"title": "Sunflowers"}})
    bridge.on_display_image({"image_url": "http://x/a.jpg"})  # reconnect re-send
    assert bridge.caption["title"] == "Sunflowers"
    bridge.on_display_image({"image_url": "http://x/b.jpg"})  # a different image
    assert bridge.caption["title"] == ""


def test_state(bridge):
    m = bridge.manager
    assert bridge.build_state()["photo"] == "None"
    m.state.update(last_displayed="2026-11-17T21:00:00+00:00", assigned_subchannel_id="photography")
    s = bridge.build_state()
    assert s["photo"] == "Untitled" and s["channel"] == "photography" and s["mimir_connected"] is True
    m.mqtt_client._last_mqtt_activity = time.monotonic() - 1000
    assert bridge.build_state()["mimir_connected"] is False


def test_buttons_off_by_default(bridge, monkeypatch):
    monkeypatch.delenv("HA_BUTTONS", raising=False)
    assert not bridge._want_buttons()


def test_platform_url_sources(bridge):
    class DC:
        platform_url = "http://mimir.local:5000/"

    bridge.manager.config.platform_url = ""
    bridge.manager.device_config = DC()
    assert bridge.platform_url() == "http://mimir.local:5000"
    bridge.manager.device_config = None
    bridge.manager.state["platform_url_override"] = "http://oak:5000"
    assert bridge.platform_url() == "http://oak:5000"


def test_buttons_auto(bridge, monkeypatch):
    monkeypatch.setenv("HA_BUTTONS", "auto")
    assert bridge._want_buttons()
    bridge.manager.capabilities["simulation_mode"] = True
    assert not bridge._want_buttons()
    monkeypatch.setenv("HA_BUTTONS", "true")
    assert bridge._want_buttons()
    bridge.manager.capabilities.update(backend="hdmi", simulation_mode=False)
    monkeypatch.setenv("HA_BUTTONS", "auto")
    assert not bridge._want_buttons()


def test_commands(bridge, monkeypatch):
    calls = []

    async def fake_new_image():
        calls.append("new_image")
        return True

    async def fake_redraw():
        calls.append("redraw")

    monkeypatch.setattr(bridge, "request_new_image", fake_new_image)
    monkeypatch.setattr(bridge, "redraw", fake_redraw)
    base = bridge.base

    async def go():
        await bridge.handle_command(f"{base}/new_image/press", "PRESS")
        await bridge.handle_command(f"{base}/redraw/press", "PRESS")
        await bridge.handle_command(f"{base}/orientation/set", "portrait_left")
        await bridge.handle_command(f"{base}/orientation/set", "sideways")  # ignored

    asyncio.run(go())
    # Changing orientation asks Mimir for a correctly shaped image.
    assert calls == ["new_image", "redraw", "new_image"]
    assert bridge.manager.orientations == ["portrait_left"]
