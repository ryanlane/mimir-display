"""Generic HDMI framebuffer backend.

This backend targets any HDMI-attached display that exposes a standard Linux
framebuffer (typically `/dev/fb0`) – *similar approach to `hyperpixelsq`* but
without HyperPixel‐specific RGB565 channel/endianness overrides beyond a small
generic set. Intended for broadly compatible full‑color HDMI panels where the
kernel KMS driver sets the mode.

Design principles:
    * Zero GUI/window dependencies (no pygame / tkinter). Pure mmap writes.
    * Auto-detect width/height/bpp via sysfs: `/sys/class/graphics/fb0`.
    * Support 16, 24, 32 bpp framebuffers.
    * Provide simple env overrides for forced bpp & byte layout for 32bpp.
    * Reuse orientation handling to expose logical (rotated) vs native geometry.

Environment variables:
    HDMI_FRAMEBUFFER          Path to fb device (default: /dev/fb0)
    HDMI_FORCE_BPP           Force treat fb as 16 / 24 / 32 (overrides sysfs)
    HDMI_8888_SEQ            Byte sequence for 32bpp (chars in {R,G,B,A,X}, default BGRX)
    HDMI_LOG_FIRST_BYTES     If int>0 log hexdump of first N bytes of first write
    HDMI_RESOLUTION          Optional override WxH (skips sysfs size if present)

Notes:
    * Unlike HyperPixel driver we do not expose RGB565 endian/channel flips –
        typical HDMI framebuffers present sane ordering.
    * If a user needs custom ordering they can still repurpose 8888 byte seq or
        force a bpp.
    * If `/dev/fb0` is not writable we fall back to simulation mode reporting.
"""
from __future__ import annotations

import os
import mmap
from dataclasses import dataclass
from PIL import Image
from mimir_display.utils.orientation import orientation_info

FB_PATH = os.getenv("HDMI_FRAMEBUFFER", "/dev/fb0")
SYSFS_BASE = "/sys/class/graphics/fb0"

_FORCE_BPP_ENV = os.getenv("HDMI_FORCE_BPP")
try:
    _FORCE_BPP = int(_FORCE_BPP_ENV) if _FORCE_BPP_ENV else None
except ValueError:
    _FORCE_BPP = None

_SEQ_8888 = os.getenv("HDMI_8888_SEQ", "BGRX").upper().strip()
if len(_SEQ_8888) != 4 or any(c not in "RGBAX" for c in _SEQ_8888):
    _SEQ_8888 = "BGRX"

try:
    _LOG_FIRST = int(os.getenv("HDMI_LOG_FIRST_BYTES", "0"))
except ValueError:
    _LOG_FIRST = 0
_LOGGED_ONCE = False

_OVERRIDE_RES = os.getenv("HDMI_RESOLUTION")
if _OVERRIDE_RES and "x" in _OVERRIDE_RES:
    try:
        _OV_W, _OV_H = [int(p) for p in _OVERRIDE_RES.lower().split("x", 1)]
    except Exception:
        _OV_W = _OV_H = None
else:
    _OV_W = _OV_H = None

_cached_geom: tuple[int, int, int] | None = None  # w,h,bpp
_cached_stride: int | None = None


def _read_sysfs(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _detect_geometry() -> tuple[int, int, int]:
    global _cached_geom
    if _cached_geom is not None:
        return _cached_geom
    if _OV_W and _OV_H:
        w, h = _OV_W, _OV_H
    else:
        virt = _read_sysfs(f"{SYSFS_BASE}/virtual_size")
        w = h = 0
        if virt and "," in virt:
            try:
                w_s, h_s = virt.split(",", 1)
                w, h = int(w_s), int(h_s)
            except Exception:
                w = h = 0
        if w <= 0 or h <= 0:
            w, h = 1280, 720  # generic fallback
    if _FORCE_BPP in (16, 24, 32):
        bpp = _FORCE_BPP
    else:
        bpp_txt = _read_sysfs(f"{SYSFS_BASE}/bits_per_pixel")
        try:
            bpp = int(bpp_txt) if bpp_txt else 32
        except Exception:
            bpp = 32
    _cached_geom = (w, h, bpp)
    return _cached_geom


def _get_stride() -> int:
    global _cached_stride
    if _cached_stride is not None:
        return _cached_stride
    path = f"{SYSFS_BASE}/stride"
    val = _read_sysfs(path)
    if val and val.isdigit():
        _cached_stride = int(val)
        return _cached_stride
    w, _h, bpp = _detect_geometry()
    bytes_pp = 2 if bpp == 16 else max(bpp // 8, 3)
    _cached_stride = w * bytes_pp
    return _cached_stride


def hardware_available() -> bool:
    try:
        return os.path.exists(FB_PATH) and os.access(FB_PATH, os.W_OK)
    except Exception:
        return False


def _convert_image(img: Image.Image, w: int, h: int, bpp: int) -> bytes:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    if img.size != (w, h):
        img = img.resize((w, h), Image.LANCZOS)
    pixels = img.load()
    if bpp == 16:
        # Generic RGB565, assume little-endian typical HDMI fb on Pi.
        out = bytearray(w * h * 2)
        i = 0
        for y in range(h):
            for x in range(w):
                r, g, b = pixels[x, y][:3]
                rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                # little-endian write
                out[i] = rgb565 & 0xFF
                out[i + 1] = (rgb565 >> 8) & 0xFF
                i += 2
        return bytes(out)
    seq = _SEQ_8888
    if bpp == 24:
        seq = ''.join([c for c in seq if c in 'RGB'][:3]) or 'BGR'
        pixel_size = 3
    else:  # 32
        pixel_size = 4
    out = bytearray(w * h * pixel_size)
    o = 0
    for y in range(h):
        for x in range(w):
            r, g, b = pixels[x, y][:3]
            cmap = {"R": r, "G": g, "B": b, "A": 255, "X": 0}
            for c in seq if bpp == 32 else seq:
                out[o] = cmap.get(c, 0)
                o += 1
            while (o % pixel_size) != 0:  # pad
                out[o] = 0
                o += 1
    return bytes(out)


def display_image(image_path: str) -> None:
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    w, h, bpp = _detect_geometry()
    img = Image.open(image_path).convert("RGB")
    data = _convert_image(img, w, h, bpp)
    stride = _get_stride()
    bytes_pp = 2 if bpp == 16 else max(bpp // 8, 3)
    fb_size = stride * h
    if not hardware_available():
        print(f"[hdmi] SIMULATION: would display {image_path} on {FB_PATH}")
        return
    with open(FB_PATH, "r+b", buffering=0) as fb:
        mm = mmap.mmap(fb.fileno(), fb_size, mmap.MAP_SHARED, mmap.PROT_WRITE)
        try:
            if stride == w * bytes_pp:
                mm.seek(0)
                mm.write(data)
            else:
                row_bytes = w * bytes_pp
                for y in range(h):
                    mm.seek(y * stride)
                    start = y * row_bytes
                    mm.write(data[start:start + row_bytes])
            global _LOGGED_ONCE
            if _LOG_FIRST and not _LOGGED_ONCE:
                sample = data[: min(len(data), _LOG_FIRST)]
                hexs = ' '.join(f"{b:02x}" for b in sample[:64])
                print(f"[hdmi] first {len(sample)} bytes: {hexs}")
                _LOGGED_ONCE = True
        finally:
            mm.close()


def is_development_mode() -> bool:
    return not hardware_available()


def get_display_capabilities() -> dict:
    w, h, bpp = _detect_geometry()
    oinfo = orientation_info(w, h)
    stride = _get_stride()
    return {
        "resolution": [oinfo.logical_width, oinfo.logical_height],
        "native_resolution": [w, h],
        "orientation": oinfo.name,
        "rotation_deg": oinfo.rotation_deg,
        "supported_formats": ["jpg", "jpeg", "png"],
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": not hardware_available(),
        "color": True,
        "pixel_format": f"RGB{bpp}",
        "backend": "hdmi",
        "bpp_mode": bpp,
        "format_8888_seq": _SEQ_8888 if bpp != 16 else None,
        "forced_bpp": _FORCE_BPP,
        "framebuffer": {
            "path": FB_PATH,
            "stride": stride,
        },
    }


def is_development_mode() -> bool:  # kept for interface parity
    return not hardware_available()


__all__ = [
    "get_display_capabilities",
    "display_image",
    "is_development_mode",
]
