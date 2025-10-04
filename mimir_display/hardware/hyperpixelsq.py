"""HyperPixel 4.0 Square framebuffer display backend.

Writes full-color images directly to /dev/fb0 using RGB565 pixel format.
Falls back to simulation logging if /dev/fb0 not writable.
"""
from __future__ import annotations

import os
import mmap
from typing import Tuple
from PIL import Image  # type: ignore

FB_PATH = os.environ.get("FRAMEBUFFER", "/dev/fb0")
FB_RESOLUTION = (720, 720)  # width, height from fbset
FB_BPP = 16  # bits per pixel

# Derived sizes
FB_WIDTH, FB_HEIGHT = FB_RESOLUTION
LINE_LENGTH = FB_WIDTH * 2  # 2 bytes per pixel RGB565
FRAMEBUFFER_SIZE = LINE_LENGTH * FB_HEIGHT


def hardware_available() -> bool:
    try:
        return os.path.exists(FB_PATH) and os.access(FB_PATH, os.W_OK)
    except Exception:
        return False


def get_display_resolution() -> Tuple[int, int]:
    return FB_RESOLUTION


def _convert_to_rgb565(img: Image.Image) -> bytes:
    """Convert a Pillow RGB/RGBA image to packed RGB565 bytes."""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    # Ensure target size
    if img.size != FB_RESOLUTION:
        img = img.resize(FB_RESOLUTION, Image.LANCZOS)
    px = img.load()
    w, h = img.size
    out = bytearray(w * h * 2)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[i] = (rgb565 >> 8) & 0xFF
            out[i + 1] = rgb565 & 0xFF
            i += 2
    return bytes(out)


def display_image(image_path: str) -> None:
    """Display image file on HyperPixel framebuffer."""
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    img = Image.open(image_path).convert("RGB")
    img = img.resize(FB_RESOLUTION, Image.LANCZOS)
    if not hardware_available():
        print(f"SIMULATION: Would display {image_path} on {FB_PATH}")
        return
    data = _convert_to_rgb565(img)
    with open(FB_PATH, "r+b", buffering=0) as fb:
        # Optionally validate size via fstat
        mm = mmap.mmap(fb.fileno(), FRAMEBUFFER_SIZE, mmap.MAP_SHARED, mmap.PROT_WRITE)
        try:
            mm.seek(0)
            mm.write(data)
        finally:
            mm.close()


def is_development_mode() -> bool:
    return not hardware_available()


def get_display_capabilities() -> dict:
    return {
        "resolution": [FB_WIDTH, FB_HEIGHT],
        "native_resolution": [FB_WIDTH, FB_HEIGHT],
        "orientation": "square",
        "rotation_deg": 0,
        "supported_formats": ["jpg", "jpeg", "png"],
        "refresh_rate_hz": 30,  # effectively immediate updates
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": not hardware_available(),
        "color": True,
        "pixel_format": "RGB565",
    }
