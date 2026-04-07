from PIL import ImageDraw

from mimir_display.content.splash import (
    _load_font,
    _status_bar_h,
    _text_size,
    build_splash,
    overlay_status,
)


def test_overlay_status_preserves_content_above_reserved_bar_small_landscape(tmp_path):
    width, height = 212, 104
    splash = build_splash(
        width=width,
        height=height,
        pair_code="ABC123",
        platform_url="http://mimir.local:5000",
        ip_address="192.168.1.50",
        logo_path=None,
    )

    splash_path = tmp_path / "startup_splash.png"
    splash.save(splash_path, format="PNG")

    updated = overlay_status(str(splash_path), "Connected", is_error=False)

    assert updated is not None

    status_bar_h = _status_bar_h(width, height)
    content_before = list(splash.crop((0, 0, width, height - status_bar_h)).getdata())
    content_after = list(updated.crop((0, 0, width, height - status_bar_h)).getdata())
    status_before = list(splash.crop((0, height - status_bar_h, width, height)).getdata())
    status_after = list(updated.crop((0, height - status_bar_h, width, height)).getdata())

    assert content_after == content_before
    assert status_after != status_before


def test_small_landscape_splash_renders_ip_band_above_pair_code():
    width, height = 212, 104
    pair_code = "ABC123"
    ip_address = "192.168.1.50"

    splash = build_splash(
        width=width,
        height=height,
        pair_code=pair_code,
        platform_url="http://mimir.local:5000",
        ip_address=ip_address,
        logo_path=None,
    )

    status_bar_h = _status_bar_h(width, height)
    content_height = height - status_bar_h
    pad = max(8, min(width, height) // 20)

    draw = ImageDraw.Draw(splash)
    code_font = _load_font(max(14, min(width, content_height) // 8), bold=True)
    small_font = _load_font(max(8, min(width, content_height) // 28), bold=False)
    code_h = _text_size(draw, pair_code, code_font)[1]
    ip_h = _text_size(draw, f"IP: {ip_address}", small_font)[1]
    ip_y = content_height - pad - code_h - pad // 2 - ip_h

    left_half_width = width // 2
    ip_region = splash.crop(
        (
            pad,
            max(0, ip_y - 2),
            left_half_width - pad,
            min(content_height, ip_y + ip_h + 2),
        )
    )

    assert any(pixel != (255, 255, 255) for pixel in ip_region.getdata())