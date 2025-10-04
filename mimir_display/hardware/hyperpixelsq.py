"""HyperPixel 4.0 Square framebuffer display backend.

Primary goals:
    * Auto-detect framebuffer resolution & bits-per-pixel via sysfs.
    * Support 16bpp RGB565 (classic HyperPixel config) and 24/32bpp XRGB8888/BGRX8888 style modes.
    * Provide runtime env toggles to correct channel or byte ordering when colors appear incorrect.
    * Expose detected + chosen format in capabilities for remote diagnostics.

ENVIRONMENT OVERRIDES
---------------------
FRAMEBUFFER                Path to framebuffer device (default: /dev/fb0)
HYPERPIXEL_FORCE_BPP       Force treat fb as 16 or 32 (overrides sysfs) e.g. "16" or "32".
HYPERPIXEL_RGB565_ENDIAN   'big' | 'little'  (default: big)
HYPERPIXEL_RGB565_CHANNEL  'rgb' | 'bgr'     (default: rgb)
HYPERPIXEL_8888_SEQ        4-char byte sequence for 32bpp, each char in {R,G,B,A,X}. Default: BGRX.
                                                     (Sequence describes in-memory byte order low->high addressing.)
HYPERPIXEL_LOG_FIRST_BYTES If set (int), log hexdump of first N bytes written once (debug).

If colors look psychedelic on a 32bpp mode, experiment with HYPERPIXEL_8888_SEQ values like:
    BGRX, BGRA, RGBX, RGBA, XBGR, XRGB, ABGR, ARGB
The common DRM little-endian XRGB8888 layout appears in memory as B G R X.
"""
from __future__ import annotations

import os
import mmap
from PIL import Image  # type: ignore

FB_PATH = os.environ.get("FRAMEBUFFER", "/dev/fb0")
SYSFS_BASE = "/sys/class/graphics/fb0"
_cached_geom: tuple[int, int, int] | None = None  # (w, h, bpp)
_cached_stride: int | None = None

# --- Configurable RGB565 handling -----------------------------------------
# Some framebuffer stacks expect little-endian byte order (low byte first) or
# a BGR channel ordering. We allow runtime overrides via env vars:
#   HYPERPIXEL_RGB565_ENDIAN  = 'big' | 'little' (default: big)
#   HYPERPIXEL_RGB565_CHANNEL = 'rgb' | 'bgr'    (default: rgb)
# Adjust these if colors appear incorrect / rainbow-ish.
_BYTE_ORDER_ENV = os.environ.get("HYPERPIXEL_RGB565_ENDIAN")
if _BYTE_ORDER_ENV is None:
    # Default to little-endian on little-endian platforms (e.g., Raspberry Pi ARM)
    import sys as _sys
    _BYTE_ORDER = "little" if _sys.byteorder == "little" else "big"
else:
    _BYTE_ORDER = _BYTE_ORDER_ENV.strip().lower()
    if _BYTE_ORDER not in {"big", "little"}:
        _BYTE_ORDER = "little"
_CHANNEL_ORDER = os.environ.get("HYPERPIXEL_RGB565_CHANNEL", "rgb").strip().lower()
if _CHANNEL_ORDER not in {"rgb", "bgr"}:
    _CHANNEL_ORDER = "rgb"

_SEQ_8888 = os.environ.get("HYPERPIXEL_8888_SEQ", "BGRX").upper().strip()
if len(_SEQ_8888) != 4 or any(c not in "RGBAX" for c in _SEQ_8888):
    _SEQ_8888 = "BGRX"  # safe default for XRGB8888 memory order under little-endian

_FORCE_BPP = os.environ.get("HYPERPIXEL_FORCE_BPP")
try:
    _FORCE_BPP_INT = int(_FORCE_BPP) if _FORCE_BPP else None
except ValueError:
    _FORCE_BPP_INT = None

_LOG_FIRST = 0
try:
    _LOG_FIRST = int(os.environ.get("HYPERPIXEL_LOG_FIRST_BYTES", "0"))
except ValueError:
    _LOG_FIRST = 0
_LOGGED_ONCE = False


def _read_sysfs_value(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _detect_geometry() -> tuple[int, int, int]:
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
    # Determine bpp
    if _FORCE_BPP_INT in (16, 24, 32):
        bpp = _FORCE_BPP_INT
    else:
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


def _framebuffer_sizes() -> tuple[int, int, int, int]:
    w, h, bpp = _detect_geometry()
    bytes_per_pixel = 2 if bpp == 16 else max(bpp // 8, 2)
    stride = _get_stride(w, bytes_per_pixel)
    fb_size = stride * h
    return w, h, bytes_per_pixel, fb_size


def _get_stride(width: int | None = None, bpp_bytes: int | None = None) -> int:
    """Return framebuffer stride (line length in bytes).

    Uses sysfs 'stride' if present; otherwise falls back to width * bpp_bytes.
    Caches result since it is static for the session.
    """
    global _cached_stride
    if _cached_stride is not None:
        return _cached_stride
    stride_path = os.path.join(SYSFS_BASE, "stride")
    val = None
    try:
        with open(stride_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
            if raw.isdigit():
                val = int(raw)
    except Exception:
        val = None
    if val is None:
        if width is None or bpp_bytes is None:
            w, _h, bpp = _detect_geometry()
            bb = 2 if bpp == 16 else max(bpp // 8, 2)
            val = w * bb
        else:
            val = width * bpp_bytes
    _cached_stride = val
    return val


def hardware_available() -> bool:
    try:
        return os.path.exists(FB_PATH) and os.access(FB_PATH, os.W_OK)
    except Exception:
        return False


def get_display_resolution() -> tuple[int, int]:
    w, h, _bpp = _detect_geometry()[:3]
    return (w, h)


def _pack_rgb565(r: int, g: int, b: int) -> int:
    """Pack 8-bit per channel RGB into RGB565 integer respecting channel swap."""
    if _CHANNEL_ORDER == "bgr":  # swap red/blue logical interpretation
        r, b = b, r
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _write_word(out: bytearray, idx: int, value: int) -> None:
    if _BYTE_ORDER == "little":
        out[idx] = value & 0xFF
        out[idx + 1] = (value >> 8) & 0xFF
    else:  # big
        out[idx] = (value >> 8) & 0xFF
        out[idx + 1] = value & 0xFF


def _convert_image(img: Image.Image, bpp: int) -> bytes:
    """Convert PIL image to framebuffer native format.

    Supports:
      * 16bpp RGB565 with configurable channel + byte order
      * 24bpp RGB/BGR (use first 3 chars of _SEQ_8888) – stored as 3 bytes
      * 32bpp XRGB8888 / variants via 4-char sequence env (default BGRX)
    """
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    w, h, _det_bpp = _detect_geometry()
    target = (w, h)
    if img.size != target:
        img = img.resize(target, Image.LANCZOS)
    px = img.load()
    w, h = img.size

    if bpp == 16:
        out = bytearray(w * h * 2)
        i = 0
        for y in range(h):
            for x in range(w):
                r, g, b = px[x, y][:3]
                rgb565 = _pack_rgb565(r, g, b)
                _write_word(out, i, rgb565)
                i += 2
        return bytes(out)

    # Prepare channel byte order for 24/32 bpp.
    seq = _SEQ_8888
    if bpp == 24:
        # Use only first 3 meaningful channels ignoring X/A positions.
        seq = ''.join([c for c in seq if c in 'RGB'][:3])
        if len(seq) != 3:
            seq = 'BGR'  # safe fallback
    bytes_per_pixel = 4 if bpp == 32 else 3
    out = bytearray(w * h * bytes_per_pixel)
    i = 0
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            channel_map = {'R': r, 'G': g, 'B': b, 'A': 255, 'X': 0}
            for c in seq:
                out[i] = channel_map.get(c, 0)
                i += 1
            # Pad remaining bytes if seq shorter than pixel size (shouldn't happen)
            while (i % bytes_per_pixel) != 0:
                out[i] = 0
                i += 1
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
    # Convert according to detected/forced bpp
    _, _, bpp = _detect_geometry()
    data = _convert_image(img, bpp)
    w, h, bpp_bytes, fb_size = _framebuffer_sizes()
    stride = _get_stride(w, bpp_bytes)
    with open(FB_PATH, "r+b", buffering=0) as fb:
        mm = mmap.mmap(fb.fileno(), fb_size, mmap.MAP_SHARED, mmap.PROT_WRITE)
        try:
            if stride == w * bpp_bytes:
                # Fast contiguous write
                mm.seek(0)
                mm.write(data)
            else:
                # Row-by-row respecting padding
                row_bytes = w * bpp_bytes
                for y in range(h):
                    off = y * stride
                    mm.seek(off)
                    start = y * row_bytes
                    mm.write(data[start:start + row_bytes])
            global _LOGGED_ONCE
            if _LOG_FIRST and not _LOGGED_ONCE:
                sample = data[: min(len(data), _LOG_FIRST)]
                hexs = ' '.join(f"{b:02x}" for b in sample[:64])
                print(f"[hyperpixelsq] first {len(sample)} bytes: {hexs}")
                _LOGGED_ONCE = True
        finally:
            mm.close()


def display_test_pattern() -> None:
    """Render a diagnostic gradient test pattern directly to the framebuffer.

    Pattern design (good for visual smoke test):
      * Horizontal axis: red (0..31) & blue diagonal mix to quickly show 5-bit channels
      * Vertical axis: green gradient (0..63) for 6-bit channel
      * Produces smooth color shifts verifying RGB565 ordering & byte endianness.

    Safe no-op if framebuffer not available.
    Controlled externally via caller (we don't gate with env var here to keep
    pure side-effect function)."""
    if not hardware_available():  # simulation / not writable
        return
    w, h, bpp = _detect_geometry()
    _w, _h, bpp_bytes, fb_size = _framebuffer_sizes()
    stride = _get_stride(_w, bpp_bytes)
    try:
        with open(FB_PATH, "r+b", buffering=0) as fb:
            mm = mmap.mmap(fb.fileno(), fb_size, mmap.MAP_SHARED, mmap.PROT_WRITE)
            try:
                if bpp == 16:
                    row_bytes = w * 2
                    for y in range(h):
                        row = bytearray(row_bytes)
                        for x in range(w):
                            r = (x * 31) // max(1, w - 1)
                            g = (y * 63) // max(1, h - 1)
                            b = ((x + y) * 31) // max(1, (w - 1) + (h - 1))
                            rgb565 = _pack_rgb565(r, g, b)
                            off = x * 2
                            if _BYTE_ORDER == "little":
                                row[off] = rgb565 & 0xFF
                                row[off + 1] = (rgb565 >> 8) & 0xFF
                            else:
                                row[off] = (rgb565 >> 8) & 0xFF
                                row[off + 1] = rgb565 & 0xFF
                        mm.seek(y * stride)
                        mm.write(row)
                else:  # 24/32 bpp pattern
                    pixel_size = 4 if bpp == 32 else 3
                    for y in range(h):
                        row = bytearray(w * pixel_size)
                        for x in range(w):
                            r = (x * 255) // max(1, w - 1)
                            g = (y * 255) // max(1, h - 1)
                            b = ((x + y) * 255) // max(1, (w - 1) + (h - 1))
                            channel_map = {'R': r, 'G': g, 'B': b, 'A': 255, 'X': 0}
                            seq = _SEQ_8888 if bpp == 32 else ''.join([c for c in _SEQ_8888 if c in 'RGB'][:3])
                            off = x * pixel_size
                            for i_c, c in enumerate(seq):
                                row[off + i_c] = channel_map.get(c, 0)
                            # pad if needed
                            for pad_i in range(len(seq), pixel_size):
                                row[off + pad_i] = 0
                        mm.seek(y * stride)
                        mm.write(row)
            finally:
                mm.close()
    except Exception:
        # Silent failure: pattern is best-effort only
        pass


def is_development_mode() -> bool:
    return not hardware_available()


def get_display_capabilities() -> dict:
    w, h, bpp = _detect_geometry()
    orientation = "square" if w == h else ("landscape" if w > h else "portrait")
    stride = _get_stride(w, 2 if bpp == 16 else max(bpp // 8, 2))
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
        "rgb565_byte_order": _BYTE_ORDER,
        "rgb565_channel_order": _CHANNEL_ORDER,
        "bpp_mode": bpp,
        "format_8888_seq": _SEQ_8888 if bpp != 16 else None,
        "detected_fb_geometry": {
            "width": w,
            "height": h,
            "bpp": bpp,
            "stride": stride,
            "path": FB_PATH,
        },
    }
