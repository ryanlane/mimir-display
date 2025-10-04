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
SYSFS_BASE = "/sys/class/graphics/fb0"
_cached_geom: Tuple[int, int, int] | None = None  # (w, h, bpp)


def _read_sysfs_value(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _detect_geometry() -> Tuple[int, int, int]:
    global _cached_geom
    if _cached_geom is not None:
        return _cached_geom
    w = h = 0
    bpp = 16
    virt = _read_sysfs_value(os.path.join(SYSFS_BASE, "virtual_size"))
    if virt and "," in virt:
        try:
            parts = virt.split(",")
            w = int(parts[0])
            h = int(parts[1])
        except Exception:
            w = h = 0
    bpp_txt = _read_sysfs_value(os.path.join(SYSFS_BASE, "bits_per_pixel"))
    if bpp_txt and bpp_txt.isdigit():
        try:
            bpp = int(bpp_txt)
        except Exception:
            bpp = 16
    # Fallback defaults if detection failed
    if w <= 0 or h <= 0:
        # Keep backward compatible default (square) but log via capabilities later
        w, h = 720, 720
    _cached_geom = (w, h, bpp)
    return _cached_geom


def _framebuffer_sizes() -> Tuple[int, int, int, int]:
    w, h, bpp = _detect_geometry()
    bytes_per_pixel = 2 if bpp == 16 else max(bpp // 8, 2)
    line_length = w * bytes_per_pixel
    fb_size = line_length * h
    return w, h, bytes_per_pixel, fb_size


def hardware_available() -> bool:
    try:
        return os.path.exists(FB_PATH) and os.access(FB_PATH, os.W_OK)
    except Exception:
        return False


def get_display_resolution() -> Tuple[int, int]:
    w, h, _bpp = _detect_geometry()[:3]
    return (w, h)


def _convert_to_rgb565(img: Image.Image) -> bytes:
    """Convert a Pillow RGB/RGBA image to packed RGB565 bytes."""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    # Ensure target size
    w, h, _ = _detect_geometry()
    target = (w, h)
    if img.size != target:
        img = img.resize(target, Image.LANCZOS)
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
    w, h, _, _ = _framebuffer_sizes()
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    if not hardware_available():
        print(f"SIMULATION: Would display {image_path} on {FB_PATH}")
        return
    data = _convert_to_rgb565(img)
    w, h, _bpp_bytes, fb_size = _framebuffer_sizes()
    with open(FB_PATH, "r+b", buffering=0) as fb:
        mm = mmap.mmap(fb.fileno(), fb_size, mmap.MAP_SHARED, mmap.PROT_WRITE)
        try:
            mm.seek(0)
            mm.write(data)
        finally:
            mm.close()


def is_development_mode() -> bool:
    return not hardware_available()


def get_display_capabilities() -> dict:
    w, h, bpp = _detect_geometry()
    orientation = "square" if w == h else ("landscape" if w > h else "portrait")
    return {
        "resolution": [w, h],
        "native_resolution": [w, h],
        "orientation": orientation,
        "rotation_deg": 0,
        "supported_formats": ["jpg", "jpeg", "png"],
        "refresh_rate_hz": 30,
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": not hardware_available(),
        "color": True,
        "pixel_format": f"RGB{bpp}",
        "backend": "hyperpixelsq",
        "detected_fb_geometry": {
            "width": w,
            "height": h,
            "bpp": bpp,
            "path": FB_PATH,
        },
    }
