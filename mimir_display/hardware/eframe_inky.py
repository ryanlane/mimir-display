# hardware/eframe_inky.py (simplified)

"""Inky display backend.

Simplified design goals:
  * Single flag `simulation_mode` (True only if ENVIRONMENT=development|dev OR init fails).
  * Minimal heuristics: we always attempt hardware unless simulation_mode already True.
  * Resolution precedence: env override -> hardware -> fallback (800x480).
  * Orientation handled via `orientation_info`.
  * Clear diagnostics via optional env flags: INKY_DEBUG, DEBUG_INKY_RESOLUTION.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import traceback

from dotenv import load_dotenv  # type: ignore
from PIL import Image  # type: ignore

from mimir_display.utils.orientation import orientation_info

logger = logging.getLogger(__name__)

load_dotenv()


# ---------------------------------------------------------------------------
# Development / simulation mode detection
# ---------------------------------------------------------------------------
def is_dev_mode() -> bool:
    """Return True iff ENVIRONMENT explicitly requests development."""
    return (os.getenv("ENVIRONMENT") or "").strip().lower() in {"development", "dev"}


simulation_mode = is_dev_mode()
if os.getenv("FORCE_INKY_HARDWARE", "").lower() in ("1", "true", "yes"):
    simulation_mode = False

# State managed lazily
inky = None  # actual Inky instance or None
_inky_init_error: Exception | None = None
_inky_initialized = False
_last_resolution_source: str | None = None  # 'override' | 'hardware' | 'fallback'


# ---------------------------------------------------------------------------
# Hardware init
# ---------------------------------------------------------------------------
def _should_attempt_hardware() -> bool:
    return not simulation_mode


def _init_inky_if_needed() -> None:
    global inky, _inky_init_error, _inky_initialized, simulation_mode
    if _inky_initialized:
        return
    _inky_initialized = True
    if not _should_attempt_hardware():
        return
    ignore_pin_busy = os.getenv("INKY_IGNORE_PIN_BUSY", "").lower() in ("1", "true", "yes")
    patched_pin_check = False
    if ignore_pin_busy:
        try:  # optional patching of gpiodevice pin check
            import gpiodevice  # type: ignore
            if hasattr(gpiodevice, "check_pins_available"):
                def _noop_check(*_a, **_k):
                    return False
                gpiodevice.check_pins_available = _noop_check  # type: ignore
                patched_pin_check = True
        except Exception:  # pragma: no cover - best effort
            pass
    try:
        from inky.auto import auto  # type: ignore
        inky = auto(ask_user=False, verbose=False)
    except ImportError as ie:  # missing package / dependency
        _inky_init_error = ie
        simulation_mode = True
        if os.getenv("INKY_DEBUG"):
            logger.warning("[INKY] ImportError -> simulation: %s", ie)
    except Exception as e:  # general init failure
        _inky_init_error = e
        # Retry once if pin contention is suspected and user allowed patching but it wasn't applied yet
        msg = str(e).lower()
        retry_done = False
        if ("pin" in msg or "busy" in msg or "in use" in msg) and ignore_pin_busy and not patched_pin_check:
            try:  # attempt late patch + retry
                import gpiodevice  # type: ignore
                if hasattr(gpiodevice, "check_pins_available"):
                    def _noop_check(*_a, **_k):
                        return False
                    gpiodevice.check_pins_available = _noop_check  # type: ignore
                    from inky.auto import auto  # type: ignore
                    inky = auto(ask_user=False, verbose=False)
                    retry_done = True
            except Exception:  # pragma: no cover
                pass
        if not retry_done:
            simulation_mode = True
            if os.getenv("INKY_DEBUG"):
                logger.warning("[INKY] Init failure -> simulation: %s", e)


if simulation_mode and os.getenv("INKY_DEBUG", "").lower() in ("1", "true", "yes"):
    print("[INKY DEBUG] simulation_mode=True (dev or init failure)")
    print(f"  ENVIRONMENT={os.getenv('ENVIRONMENT')}")
    spec = importlib.util.find_spec("inky")
    print(f"  inky module spec: {spec}")
    if _inky_init_error:
        print("  init_error_type=", type(_inky_init_error).__name__)
        print("  init_error=", _inky_init_error)
        traceback.print_exception(type(_inky_init_error), _inky_init_error, _inky_init_error.__traceback__)


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------
def _parse_resolution_override() -> tuple[int, int] | None:
    raw = (os.getenv("DISPLAY_NATIVE_RESOLUTION") or os.getenv("DISPLAY_RESOLUTION") or "").strip()
    if not raw:
        return None
    raw = raw.lower().replace(" ", "")
    if "x" not in raw:
        return None
    try:
        w_s, h_s = raw.split("x", 1)
        w, h = int(w_s), int(h_s)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:  # pragma: no cover - invalid user input
        return None
    return None


def _get_inky_resolution_with_source() -> tuple[int, int, str]:
    global _last_resolution_source
    # 1) Override
    override = _parse_resolution_override()
    if override:
        _last_resolution_source = "override"
        if os.getenv("DEBUG_INKY_RESOLUTION"):
            logger.info("[RESOLUTION] override=%s", override)
        return override[0], override[1], _last_resolution_source
    # 2) Hardware
    _init_inky_if_needed()
    if not simulation_mode and inky is not None:
        try:
            res = getattr(inky, "resolution", None)
            if isinstance(res, (list, tuple)) and len(res) == 2:
                w, h = int(res[0]), int(res[1])
                if w > 0 and h > 0:
                    _last_resolution_source = "hardware"
                    return w, h, _last_resolution_source
            w_attr = getattr(inky, "width", None)
            h_attr = getattr(inky, "height", None)
            if isinstance(w_attr, int) and isinstance(h_attr, int) and w_attr > 0 and h_attr > 0:
                _last_resolution_source = "hardware"
                return w_attr, h_attr, _last_resolution_source
        except Exception as e:  # pragma: no cover - defensive
            if os.getenv("DEBUG_INKY_RESOLUTION"):
                logger.warning("[RESOLUTION] hardware detection failed: %s", e)
    # 3) Fallback
    _last_resolution_source = "fallback"
    return 800, 480, _last_resolution_source


def get_inky_resolution() -> tuple[int, int]:  # public compatibility wrapper
    w, h, _ = _get_inky_resolution_with_source()
    return w, h


def get_inky_colour_variant() -> str:
    _init_inky_if_needed()
    if simulation_mode or inky is None:
        return os.getenv("FAKE_INKY_COLOR", "simulated")
    return getattr(inky, "colour", getattr(inky, "color", "unknown"))


def show_on_inky(image_path: str) -> None:
    global simulation_mode
    _init_inky_if_needed()
    if simulation_mode or inky is None:
        logger.info("[DEV] Would display: %s", image_path)
        return
    logger.info("Updating image: %s", image_path)
    img = Image.open(image_path)
    inky.set_image(img)
    try:
        inky.set_border(inky.BLACK)  # some variants
    except Exception:  # pragma: no cover - not all support border
        pass
    logger.info("Inky refresh started …")
    try:
        inky.show()
        logger.info("Inky refresh complete")
    except SystemExit as se:  # pin contention or low-level abort
        logger.error("[INKY] SystemExit during refresh: %s -- switching to simulation", se)
        simulation_mode = True
    except Exception as e:
        logger.error("[INKY] Unexpected refresh exception: %s", e)
        simulation_mode = True


# ---------------------------------------------------------------------------
# Backend interface
# ---------------------------------------------------------------------------
def get_display_capabilities() -> dict:
    w, h, src = _get_inky_resolution_with_source()
    oinfo = orientation_info(w, h)
    return {
        "resolution": [oinfo.logical_width, oinfo.logical_height],
        "native_resolution": [w, h],
        "resolution_source": src,
        "orientation": oinfo.name,
        "rotation_deg": oinfo.rotation_deg,
        "supported_formats": ["jpg", "jpeg", "png"],
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": bool(simulation_mode or inky is None),
        "backend": "inky",
        "color_variant": get_inky_colour_variant(),
        "init_error": type(_inky_init_error).__name__ if _inky_init_error else None,
    }


def display_image(image_path: str) -> None:
    show_on_inky(image_path)


def is_development_mode() -> bool:
    return simulation_mode


__all__ = [
    "get_display_capabilities",
    "display_image",
    "is_development_mode",
    "get_inky_resolution",
    "show_on_inky",
]
