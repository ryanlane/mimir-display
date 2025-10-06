"""RGB LED Matrix backend using hzeller/rpi-rgb-led-matrix bindings.

Prerequisites:
  * The underlying C++ library and Python bindings must be installed per
    https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/matrix-setup
    and https://github.com/hzeller/rpi-rgb-led-matrix

Environment Variables (all optional):
    RGBMATRIX_ROWS               Panel rows (height) (16, 32, 64, 128 ...). Default: 32
    RGBMATRIX_WIDTH              Panel width override for single panel setups (16, 32, 64 ...). If unset we infer.
    RGBMATRIX_CHAIN_LENGTH       Number of daisy-chained panels (widens display). Default: 1
    RGBMATRIX_PARALLEL           Parallel chains (advanced). Default: 1
    RGBMATRIX_HARDWARE_MAPPING   Hardware mapping string (e.g. "adafruit-hat", "adafruit-hat-pwm", "regular"). Default: "regular"
    RGBMATRIX_GPIO_SLOWDOWN      0..4 slowdown for timing sensitive setups. Default: unset (library default)
    RGBMATRIX_PWM_BITS           PWM bit-depth (1..11 typical). Default: unset (library default)
    RGBMATRIX_BRIGHTNESS         1..100 panel brightness. Default: 50
    RGBMATRIX_LIMIT_FPS          Software cap on refresh rate. Default: unset
    RGBMATRIX_PIXEL_MAPPER       Pixel mapper string (e.g. "Rotate:90"). We rely on our own rotation logic; leave unset.
    DISPLAY_ORIENTATION          Re‑used orientation variable (landscape/portrait_left/portrait_right/square)

Capabilities strategy:
  * We treat the *logical* resolution as (rows * chain_length * width_per_panel, rows) typical for standard panels
    but cannot know the width without the rows vs. cols specification. Most LED matrices are 32x64 (HxW) or 64x64.
    We expose both native and logical dimensions after querying matrix.width/height from the library once instantiated.
  * Orientation is applied virtually using Pillow image rotation before pushing to the hardware.

Error handling:
  * If the rgbmatrix module or construction fails we mark simulation_mode True and just log what would display.

This backend only supports RGB color images (jpg/png) and ignores alpha.
"""
from __future__ import annotations

import logging
import os

from PIL import Image  # type: ignore

from mimir_display.utils.orientation import orientation_info

logger = logging.getLogger(__name__)

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions  # type: ignore
except Exception:  # pragma: no cover - library not installed
    RGBMatrix = None  # type: ignore
    RGBMatrixOptions = None  # type: ignore

_matrix: RGBMatrix | None = None  # type: ignore[name-defined]
_init_error: Exception | None = None
_initialized = False
_simulation_mode = False
_cached_resolution: tuple[int, int] | None = None


def _build_options() -> RGBMatrixOptions:  # type: ignore[name-defined]
    assert RGBMatrixOptions is not None
    opts = RGBMatrixOptions()
    # Basic geometry
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except ValueError:
            return default
    opts.rows = _env_int("RGBMATRIX_ROWS", 32)
    opts.chain_length = _env_int("RGBMATRIX_CHAIN_LENGTH", 1)
    opts.parallel = _env_int("RGBMATRIX_PARALLEL", 1)
    opts.hardware_mapping = os.getenv("RGBMATRIX_HARDWARE_MAPPING", "regular")
    # Optional tuning parameters
    if (val := os.getenv("RGBMATRIX_GPIO_SLOWDOWN")) is not None:
        try:
            opts.gpio_slowdown = int(val)
        except ValueError:
            pass
    if (val := os.getenv("RGBMATRIX_PWM_BITS")) is not None:
        try:
            opts.pwm_bits = int(val)
        except ValueError:
            pass
    if (val := os.getenv("RGBMATRIX_LIMIT_FPS")) is not None:
        try:
            opts.limit_refresh_rate_hz = int(val)
        except ValueError:
            pass
    # brightness handled at matrix level later if provided
    return opts


def _init_matrix() -> None:
    global _initialized, _matrix, _init_error, _simulation_mode, _cached_resolution
    if _initialized:
        return
    _initialized = True
    if RGBMatrix is None or RGBMatrixOptions is None:
        _simulation_mode = True
        return
    try:
        opts = _build_options()
        _matrix = RGBMatrix(options=opts)
        # brightness
        try:
            b_str = os.getenv("RGBMATRIX_BRIGHTNESS")
            if b_str:
                b_int = max(1, min(100, int(b_str)))
                _matrix.brightness = b_int  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass
        # Cache width/height
        _cached_resolution = (_matrix.width, _matrix.height)  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover - hardware init failure
        _init_error = e
        _simulation_mode = True
        logger.warning("[rgbmatrix] init failed -> simulation: %s", e)


def _get_native_resolution() -> tuple[int, int]:
    _init_matrix()
    if _cached_resolution:
        return _cached_resolution
    rows = int(os.getenv("RGBMATRIX_ROWS", "32") or 32)
    chain = int(os.getenv("RGBMATRIX_CHAIN_LENGTH", "1") or 1)
    width_override_env = os.getenv("RGBMATRIX_WIDTH")
    width_override = None
    try:
        if width_override_env:
            width_override = int(width_override_env)
    except ValueError:
        width_override = None

    if width_override and width_override > 0:
        base_width = width_override
    else:
        # Improved heuristic:
        #  * Square panels (16,32,64,128) frequently WxH = rows
        #  * 32x64 panels are common (height=32 width=64)
        #  * If rows in {16,32,64,128} and no explicit width, prefer square assumption first.
        if rows in {16, 32, 64, 128}:
            base_width = rows
        else:
            # Fallback to typical 64 width for rows 32/64 (already handled) else rows.
            base_width = rows
        # Special case: if user explicitly set ENV HxW pair historically (rows=32 but wants 64) they can set RGBMATRIX_WIDTH=64.
    width = base_width * chain
    return (width, rows)


def display_image(image_path: str) -> None:
    _init_matrix()
    if not os.path.exists(image_path):
        logger.error("[rgbmatrix] image not found: %s", image_path)
        return
    img = Image.open(image_path)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")  # drop alpha

    native_w, native_h = _get_native_resolution()
    oinfo = orientation_info(native_w, native_h)

    # Resize preserving aspect then center crop/pad (simpler: direct resize to panel)
    if img.size != (native_w, native_h):
        img = img.resize((native_w, native_h), Image.LANCZOS)

    # Apply rotation so final appears correct physically
    if oinfo.rotation_deg:
        # rotation_deg is clockwise; Pillow rotate is counter-clockwise degrees
        ccw = (360 - oinfo.rotation_deg) % 360
        if ccw:
            img = img.rotate(ccw, expand=False)

    if _simulation_mode or _matrix is None:
        logger.info("[rgbmatrix][SIM] Would display %s (%sx%s)", image_path, native_w, native_h)
        return

    try:
        # The library expects image sized to panel; SetImage handles conversion
        _matrix.SetImage(img)  # type: ignore[func-returns-value]
    except Exception as e:  # pragma: no cover
        logger.error("[rgbmatrix] SetImage failed: %s", e)


def is_development_mode() -> bool:
    _init_matrix()
    return _simulation_mode


def get_display_capabilities() -> dict:
    native_w, native_h = _get_native_resolution()
    oinfo = orientation_info(native_w, native_h)
    return {
        "resolution": [oinfo.logical_width, oinfo.logical_height],
        "native_resolution": [native_w, native_h],
        "orientation": oinfo.name,
        "rotation_deg": oinfo.rotation_deg,
        "supported_formats": ["jpg", "jpeg", "png"],
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": is_development_mode(),
        "backend": "rgbmatrix",
        "color": True,
        "init_error": type(_init_error).__name__ if _init_error else None,
        "hardware_mapping": os.getenv("RGBMATRIX_HARDWARE_MAPPING", "regular"),
        "rows": int(os.getenv("RGBMATRIX_ROWS", "32") or 32),
        "chain_length": int(os.getenv("RGBMATRIX_CHAIN_LENGTH", "1") or 1),
        "parallel": int(os.getenv("RGBMATRIX_PARALLEL", "1") or 1),
        "brightness": int(os.getenv("RGBMATRIX_BRIGHTNESS", "50") or 50),
        "width_override": os.getenv("RGBMATRIX_WIDTH") or None,
    }


__all__ = [
    "get_display_capabilities",
    "display_image",
    "is_development_mode",
]
