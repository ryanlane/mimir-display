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
    HDMI_FORCE_BPP            Force treat fb as 16 / 24 / 32 (overrides sysfs)
    HDMI_8888_SEQ             Byte sequence for 32bpp (chars in {R,G,B,A,X}, default BGRX)
    HDMI_LOG_FIRST_BYTES      If int>0 log hexdump of first N bytes of first write
    HDMI_RESOLUTION           Optional override WxH (skips sysfs size if present)
    HDMI_FILL_MODE            Image scaling strategy: 'contain' (default, preserve aspect
                              ratio with black letterbox/pillarbox), 'stretch' (distort to
                              exact screen), 'cover' (fill and crop excess), 'center'
                              (no scaling; center the original image on a black canvas; if
                              larger than screen it is center-cropped).

Notes:
    * Unlike HyperPixel driver we do not expose RGB565 endian/channel flips –
        typical HDMI framebuffers present sane ordering.
    * If a user needs custom ordering they can still repurpose 8888 byte seq or
        force a bpp.
    * If `/dev/fb0` is not writable we fall back to simulation mode reporting.
"""
from __future__ import annotations

import mmap
import os

from PIL import Image

from mimir_display.utils.orientation import orientation_info

FB_PATH = os.getenv("HDMI_FRAMEBUFFER", "/dev/fb0")
SYSFS_BASE = "/sys/class/graphics/fb0"

def _env_force_bpp() -> int | None:
    """Read HDMI_FORCE_BPP at call time (import-time snapshots break env overrides)."""
    raw = os.getenv("HDMI_FORCE_BPP")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None

_SEQ_8888 = os.getenv("HDMI_8888_SEQ", "BGRX").upper().strip()
if len(_SEQ_8888) != 4 or any(c not in "RGBAX" for c in _SEQ_8888):
    _SEQ_8888 = "BGRX"

try:
    _LOG_FIRST = int(os.getenv("HDMI_LOG_FIRST_BYTES", "0"))
except ValueError:
    _LOG_FIRST = 0
_LOGGED_ONCE = False

def _env_override_resolution() -> tuple[int | None, int | None]:
    """Read HDMI_RESOLUTION at call time (import-time snapshots break env overrides)."""
    raw = os.getenv("HDMI_RESOLUTION")
    if raw and "x" in raw:
        try:
            w, h = (int(p) for p in raw.lower().split("x", 1))
            return w, h
        except Exception:
            return None, None
    return None, None


def _geom_env_key() -> tuple[str | None, str | None]:
    return (os.getenv("HDMI_RESOLUTION"), os.getenv("HDMI_FORCE_BPP"))


_cached_geom: tuple[int, int, int] | None = None  # w,h,bpp
_cached_geom_key: tuple[str | None, str | None] | None = None
_cached_stride: int | None = None

_FILL_MODE = os.getenv("HDMI_FILL_MODE", "contain").strip().lower()
if _FILL_MODE not in {"contain", "stretch", "cover", "center"}:
    _FILL_MODE = "contain"


def _read_sysfs(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _detect_geometry() -> tuple[int, int, int]:
    global _cached_geom, _cached_geom_key, _cached_stride
    env_key = _geom_env_key()
    if _cached_geom is not None and _cached_geom_key == env_key:
        return _cached_geom
    _cached_stride = None  # stride derives from geometry; recompute alongside it
    ov_w, ov_h = _env_override_resolution()
    if ov_w and ov_h:
        w, h = ov_w, ov_h
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
    force_bpp = _env_force_bpp()
    if force_bpp in (16, 24, 32):
        bpp = force_bpp
    else:
        bpp_txt = _read_sysfs(f"{SYSFS_BASE}/bits_per_pixel")
        try:
            bpp = int(bpp_txt) if bpp_txt else 32
        except Exception:
            bpp = 32
    _cached_geom = (w, h, bpp)
    _cached_geom_key = env_key
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


def _prepare_canvas(img: Image.Image, w: int, h: int) -> Image.Image:
    """Return an RGB image exactly (w,h) using selected fill mode.

    Modes:
        contain: scale to fit inside, keeping aspect, black bars fill remainder.
        stretch: scale to exact size (current legacy behavior, may distort).
        cover:   scale to cover entire screen, cropping overflow, centered.
        center:  no scaling; paste image centered on black canvas. If image is
                 larger than the target, crop the central region to fit.
    """
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    else:
        # If RGBA ensure we composite onto black to avoid white fringing.
        if img.mode == "RGBA":
            bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
            bg.alpha_composite(img)
            img = bg.convert("RGB")

    if _FILL_MODE == "stretch":
        if img.size != (w, h):
            return img.resize((w, h), Image.LANCZOS)
        return img

    # Center mode: no scaling, just center (or center-crop) on black background
    if _FILL_MODE == "center":
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        src_w, src_h = img.size
        # If bigger than target, crop central portion first
        if src_w > w or src_h > h:
            left = max(0, (src_w - w) // 2)
            top = max(0, (src_h - h) // 2)
            img = img.crop((left, top, left + min(w, src_w - left), top + min(h, src_h - top)))
            src_w, src_h = img.size
        off_x = (w - src_w) // 2
        off_y = (h - src_h) // 2
        canvas.paste(img, (off_x, off_y))
        return canvas

    src_w, src_h = img.size
    if src_w == w and src_h == h:
        return img if img.mode == "RGB" else img.convert("RGB")

    # Compute scale factors
    scale_x = w / src_w
    scale_y = h / src_h
    if _FILL_MODE == "contain":
        scale = min(scale_x, scale_y)
    else:  # cover
        scale = max(scale_x, scale_y)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    # For contain -> paste centered onto black canvas
    if _FILL_MODE == "contain":
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        off_x = (w - new_w) // 2
        off_y = (h - new_h) // 2
        canvas.paste(resized, (off_x, off_y))
        return canvas

    # cover: crop center to screen size
    if new_w == w and new_h == h:
        return resized.convert("RGB") if resized.mode != "RGB" else resized
    # Crop box
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    box = (left, top, left + w, top + h)
    cropped = resized.crop(box)
    return cropped.convert("RGB") if cropped.mode != "RGB" else cropped


_FAST_ENCODE_OK: dict[tuple, bool] = {}


def _rgb565_bytes(img: Image.Image) -> bytes:
    """RGB565 little-endian via C-level channel arithmetic:
    hi = (r & F8) | (g >> 5), lo = ((g & 1C) << 3) | (b >> 3), interleaved
    lo,hi per pixel by an LA merge. Bit fields never overlap, so the
    saturating adds are exact."""
    from PIL import ImageChops
    r, g, b = img.split()
    hi = ImageChops.add(r.point(lambda v: v & 0xF8), g.point(lambda v: v >> 5))
    lo = ImageChops.add(g.point(lambda v: (v & 0x1C) << 3), b.point(lambda v: v >> 3))
    return Image.merge("LA", (lo, hi)).tobytes()


def _fast_encode(img: Image.Image, bpp: int) -> bytes:
    """C-speed conversion via Pillow raw encoders (see _convert_image)."""
    img = img.convert("RGB")
    if bpp == 16:
        return _rgb565_bytes(img)  # RGB565, little-endian
    if bpp == 24:
        seq = ''.join([c for c in _SEQ_8888 if c in 'RGB'][:3]) or 'BGR'
        if seq not in ("RGB", "BGR"):
            raise ValueError(f"no fast encoder for 24bpp seq {seq!r}")
        return img.tobytes("raw", seq)
    raw = {"BGRX": "BGRX", "RGBX": "RGBX", "XRGB": "XRGB", "XBGR": "XBGR"}.get(_SEQ_8888)
    if raw is None:
        raise ValueError(f"no fast encoder for 32bpp seq {_SEQ_8888!r}")
    return img.tobytes("raw", raw)


def _fast_encode_verified(bpp: int) -> bool:
    """Trust the fast path only after a probe converts byte-identically
    to the reference per-pixel loop for the active configuration."""
    key = (bpp, _SEQ_8888)
    ok = _FAST_ENCODE_OK.get(key)
    if ok is None:
        try:
            probe = Image.new("RGB", (4, 2))
            probe.putdata([(0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0),
                           (0, 0, 255), (17, 130, 213), (250, 8, 121), (66, 66, 66)])
            ok = _fast_encode(probe, bpp) == _convert_pixels_slow(probe, bpp)
        except Exception:
            ok = False
        _FAST_ENCODE_OK[key] = ok
    return ok


def _convert_image(img: Image.Image, w: int, h: int, bpp: int) -> bytes:
    img = _prepare_canvas(img, w, h)
    if _fast_encode_verified(bpp):
        return _fast_encode(img, bpp)
    return _convert_pixels_slow(img, bpp)


def _convert_pixels_slow(img: Image.Image, bpp: int) -> bytes:
    """Reference per-pixel conversion — correct for any layout, slow."""
    w, h = img.size
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


_SIM_LOGGED = False


def display_pil(img: Image.Image) -> None:
    """Write a PIL image straight to the framebuffer — no file round-trip.

    This is the animation fast path: AnimationPlayer calls it per frame,
    so it must not touch disk. Contract matches display_image (any-size
    image in; conversion/letterboxing to native geometry happens here).
    """
    display_frame_bytes(prepare_frame(img))


def prepare_frame(img: Image.Image) -> bytes:
    """Convert a PIL image to native framebuffer bytes.

    Animation playback pre-converts every frame once — the Python pixel
    conversion is far too slow to run per frame inside the play loop."""
    w, h, bpp = _detect_geometry()
    return _convert_image(img, w, h, bpp)


def display_frame_bytes(data: bytes) -> None:
    """Write pre-converted framebuffer bytes (from prepare_frame)."""
    w, h, bpp = _detect_geometry()
    _write_framebuffer(data, w, h, bpp)


def display_image(image_path: str) -> None:
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    w, h, bpp = _detect_geometry()
    img = Image.open(image_path)
    data = _convert_image(img, w, h, bpp)
    _write_framebuffer(data, w, h, bpp)


def _write_framebuffer(data: bytes, w: int, h: int, bpp: int) -> None:
    stride = _get_stride()
    bytes_pp = 2 if bpp == 16 else max(bpp // 8, 3)
    fb_size = stride * h
    if not hardware_available():
        global _SIM_LOGGED
        if not _SIM_LOGGED:
            print(f"[hdmi] SIMULATION: would write frames to {FB_PATH} (logged once)")
            _SIM_LOGGED = True
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


def get_display_capabilities() -> dict:
    w, h, bpp = _detect_geometry()
    oinfo = orientation_info(w, h)
    stride = _get_stride()
    return {
        "resolution": [oinfo.logical_width, oinfo.logical_height],
        "native_resolution": [w, h],
        "orientation": oinfo.name,
        "rotation_deg": oinfo.rotation_deg,
        # webp/gif accepted since Pillow decodes them; animated files
        # currently display their first frame (see DisplayManager).
        "supported_formats": ["jpg", "jpeg", "png", "webp", "gif"],
        # HDMI panels can refresh fast enough for animated content once
        # playback lands; advertised now so the platform can negotiate.
        "supports_animation": True,
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": not hardware_available(),
        "color": True,
        "pixel_format": f"RGB{bpp}",
        "backend": "hdmi",
        "bpp_mode": bpp,
        "format_8888_seq": _SEQ_8888 if bpp != 16 else None,
        "forced_bpp": _env_force_bpp(),
        "framebuffer": {
            "path": FB_PATH,
            "stride": stride,
        },
        "fill_mode": _FILL_MODE,
    }


def is_development_mode() -> bool:  # kept for interface parity
    return not hardware_available()


__all__ = [
    "get_display_capabilities",
    "display_image",
    "is_development_mode",
]
