"""Animated WebP/GIF handling — the client must accept the formats and
deliberately display the first frame (playback is not yet supported)."""
import logging

from PIL import Image


def _make_animated_webp(path, first_color=(255, 0, 0), second_color=(0, 0, 255)):
    frames = [
        Image.new("RGB", (64, 40), first_color),
        Image.new("RGB", (64, 40), second_color),
    ]
    frames[0].save(path, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=100, loop=0)


def test_hdmi_capabilities_declare_webp_and_gif(monkeypatch):
    monkeypatch.setenv("DISPLAY_BACKEND", "hdmi")
    monkeypatch.setenv("HDMI_RESOLUTION", "640x360")
    monkeypatch.setenv("HDMI_FORCE_BPP", "24")
    from mimir_display.hardware import get_display_capabilities  # type: ignore

    caps = get_display_capabilities()
    assert "webp" in caps["supported_formats"]
    assert "gif" in caps["supported_formats"]


def test_display_from_file_uses_first_frame_of_animation(tmp_path):
    from mimir_display.content.display import DisplayManager

    webp_path = tmp_path / "anim.webp"
    _make_animated_webp(webp_path)

    manager = DisplayManager(
        capabilities={"resolution": [64, 40], "orientation": "landscape"},
        cache_dir=str(tmp_path),
        logger=logging.getLogger("test"),
    )

    rendered = {}
    manager._hw_display_image = lambda img: rendered.setdefault("img", img.copy())

    manager.display_from_file(webp_path)

    img = rendered["img"]
    # Frame 0 is red; frame 1 is blue. Sample the center pixel.
    r, g, b = img.getpixel((img.width // 2, img.height // 2))
    assert r > 200 and b < 60, f"expected first (red) frame, got rgb({r},{g},{b})"
