# hardware/eframe_inky.py

import os
import sys
import logging
import importlib.util
import traceback
from PIL import Image  # type: ignore
from dotenv import load_dotenv  # type: ignore
from mimir_display.utils.orientation import orientation_info


logger = logging.getLogger(__name__)

load_dotenv()

def is_dev_mode():
    """Check if application is running in development mode based on environment variable"""
    env = os.getenv('ENVIRONMENT', '').lower()
    # Also check for common development indicators
    dev_indicators = os.getenv('DEV_MODE', '').lower() in ('true', '1', 'yes')
    no_hardware = os.getenv('NO_HARDWARE', '').lower() in ('true', '1', 'yes')
    return env in ('development', 'dev') or dev_indicators or no_hardware

use_fake = is_dev_mode()

# Allow explicit override to force hardware attempt even if dev indicators set
if os.getenv("FORCE_INKY_HARDWARE", "").lower() in ("1", "true", "yes"):  # manual override
    use_fake = False

inky = None
_inky_init_error = None
_inky_initialized = False

def _want_inky_backend() -> bool:
    """Determine if the inky backend is actually requested.

    We only attempt hardware initialization (and therefore only emit warnings)
    if the process explicitly selected or autodetected the inky backend.
    Signals (checked in order):
      1. BACKEND env var equals 'inky' (case-insensitive)
      2. FORCE_INKY_HARDWARE explicitly set
      3. No other backend forced AND no framebuffer hardware present (heuristic)
    """
    backend_env = (os.getenv("BACKEND") or os.getenv("DISPLAY_BACKEND") or "").strip().lower()
    if backend_env == "inky":
        return True
    if os.getenv("FORCE_INKY_HARDWARE", "").lower() in ("1", "true", "yes"):
        return True
    # Heuristic: if a framebuffer path exists and is writable, likely not inky; don't auto-init.
    fb_path = os.getenv("FRAMEBUFFER", "/dev/fb0")
    if os.path.exists(fb_path) and os.access(fb_path, os.W_OK):
        # Allow explicit CLI override
        if "--backend" in sys.argv:
            try:
                idx = sys.argv.index("--backend")
                if idx + 1 < len(sys.argv) and sys.argv[idx + 1] == "inky":
                    return True
            except ValueError:
                pass
        return False
    # Otherwise defer to environment development indicators; only try if not explicitly dev fake.
    return not is_dev_mode()


def _init_inky_if_needed():
    global inky, _inky_init_error, _inky_initialized, use_fake
    if _inky_initialized:
        return
    _inky_initialized = True
    if use_fake:
        return  # Respect explicit dev/fake choice without logging noise.
    if not _want_inky_backend():
        # Backend not selected; remain silent and in lazy state.
        return
    # Optional: ignore pin busy checks if user opts in (e.g. SPI controller legitimately owns CS0)
    ignore_pin_busy = os.getenv("INKY_IGNORE_PIN_BUSY", "").lower() in ("1", "true", "yes")
    patched_pin_check = False
    if ignore_pin_busy:
        try:  # Lazy/defensive – only patch if gpiodevice available
            import gpiodevice  # type: ignore
            if hasattr(gpiodevice, "check_pins_available"):
                _orig_check = gpiodevice.check_pins_available  # type: ignore
                def _noop_check(*_a, **_k):
                    return False  # "pins not busy" signal
                gpiodevice.check_pins_available = _noop_check  # type: ignore
                patched_pin_check = True
        except Exception:
            pass
    try:
        from inky.auto import auto  # type: ignore
        inky = auto(ask_user=False, verbose=False)  # Don't ask user in non-interactive mode
    except ImportError as ie:
        _inky_init_error = ie
        # Only warn if user explicitly asked for inky.
        backend_env_local = (os.getenv("BACKEND") or os.getenv("DISPLAY_BACKEND") or "").strip().lower()
        if backend_env_local == "inky" or os.getenv("FORCE_INKY_HARDWARE", "").lower() in ("1", "true", "yes"):
            print("Warning: inky package not installed or failed to import, running in fake mode")
        use_fake = True
    except Exception as e:
        _inky_init_error = e
        backend_env_local = (os.getenv("BACKEND") or os.getenv("DISPLAY_BACKEND") or "").strip().lower()
        # Second-chance retry: if failure looks like pin contention and we have not yet patched, attempt patch+retry
        msg = str(e).lower()
        retry_done = False
        if ("pin" in msg or "busy" in msg or "in use" in msg) and ignore_pin_busy and not patched_pin_check:
            try:
                import gpiodevice  # type: ignore
                if hasattr(gpiodevice, "check_pins_available"):
                    def _noop_check(*_a, **_k):
                        return False
                    gpiodevice.check_pins_available = _noop_check  # type: ignore
                    from inky.auto import auto  # type: ignore
                    inky = auto(ask_user=False, verbose=False)
                    retry_done = True
            except Exception:
                pass
        if not retry_done:
            if backend_env_local == "inky" or os.getenv("FORCE_INKY_HARDWARE", "").lower() in ("1", "true", "yes"):
                print(f"Warning: Could not initialize inky hardware: {e}, running in fake mode")
            use_fake = True

if use_fake and os.getenv("DEBUG_INKY_IMPORT", "").lower() in ("1", "true", "yes"):
    print("[INKY DEBUG] use_fake=True. Environment details:")
    print(f"  ENVIRONMENT={os.getenv('ENVIRONMENT')}")
    print(f"  DEV_MODE={os.getenv('DEV_MODE')}")
    print(f"  NO_HARDWARE={os.getenv('NO_HARDWARE')}")
    spec = importlib.util.find_spec("inky")
    print(f"  inky module spec: {spec}")
    if _inky_init_error:
        print("  Inky init error type:", type(_inky_init_error).__name__)
        print("  Inky init error:", _inky_init_error)
        traceback.print_exception(type(_inky_init_error), _inky_init_error, _inky_init_error.__traceback__)

def _parse_resolution_override() -> list[int] | None:
    """Parse resolution override from env.

    Supports DISPLAY_NATIVE_RESOLUTION or DISPLAY_RESOLUTION in form WIDTHxHEIGHT (case-insensitive).
    Returns list [w, h] or None if not set/invalid.
    """
    raw = os.getenv("DISPLAY_NATIVE_RESOLUTION") or os.getenv("DISPLAY_RESOLUTION")
    if not raw:
        return None
    raw = raw.lower().replace(" ", "")
    if "x" not in raw:
        return None
    try:
        w_str, h_str = raw.split("x", 1)
        w, h = int(w_str), int(h_str)
        if w > 0 and h > 0:
            return [w, h]
    except Exception:
        return None
    return None


def get_inky_resolution():
    """Determine panel resolution.

    Priority:
      1. Explicit environment override (DISPLAY_NATIVE_RESOLUTION / DISPLAY_RESOLUTION)
      2. Hardware provided inky.resolution tuple (preferred authoritative source)
      3. Fallback default (800x480)

    Returns:
        (width, height) as a tuple of ints in native landscape order.
    """
    # 1) Override
    override = _parse_resolution_override()
    if override:
        if os.getenv("DEBUG_INKY_RESOLUTION"):
            logger.info("[RESOLUTION] Using override from env: %s", override)
        return (int(override[0]), int(override[1]))

    # 2) Hardware
    _init_inky_if_needed()
    if not use_fake and inky is not None:
        try:
            res = getattr(inky, "resolution", None)
            if isinstance(res, (list, tuple)) and len(res) == 2:
                w, h = int(res[0]), int(res[1])
                if w > 0 and h > 0:
                    if os.getenv("DEBUG_INKY_RESOLUTION"):
                        logger.info("[RESOLUTION] Using hardware detected resolution: (%d, %d)", w, h)
                    return (w, h)
            # Some versions might expose width/height separately
            maybe_w = getattr(inky, "width", None)
            maybe_h = getattr(inky, "height", None)
            if isinstance(maybe_w, int) and isinstance(maybe_h, int) and maybe_w > 0 and maybe_h > 0:
                if os.getenv("DEBUG_INKY_RESOLUTION"):
                    logger.info("[RESOLUTION] Using hardware width/height attributes: (%d, %d)", maybe_w, maybe_h)
                return (maybe_w, maybe_h)
        except Exception as e:
            if os.getenv("DEBUG_INKY_RESOLUTION"):
                logger.warning("[RESOLUTION] Hardware resolution detection failed: %s", e)

    # 3) Fallback
    if os.getenv("DEBUG_INKY_RESOLUTION"):
        logger.info("[RESOLUTION] Falling back to default resolution: (800, 480)")
    return (800, 480)

def get_inky_colour_variant():
    _init_inky_if_needed()
    if use_fake or inky is None:
        return os.getenv("FAKE_INKY_COLOR", "simulated")
    return getattr(inky, "colour", getattr(inky, "color", "unknown"))

def show_on_inky(imagepath):
    _init_inky_if_needed()
    if use_fake or inky is None:
        logger.info("[DEV] Would display: %s", imagepath)
        return

    logger.info("Updating image: %s", imagepath)

    img = Image.open(imagepath)
    inky.set_image(img)
    inky.set_border(inky.BLACK)

    logger.info("Inky refresh started (~20–35s) …")
    try:
        inky.show()
        logger.info("Inky refresh complete")
    except SystemExit as se:  # Pin contention or gpiodevice fatal check
        global use_fake
        logger.error("[INKY] Hardware update aborted (pin contention or setup error): %s", se)
        # Switch to simulation for subsequent calls instead of killing service
        use_fake = True
    except Exception as e:  # General failure path
        logger.error("[INKY] Unexpected exception during refresh: %s", e)


# ---- Backend interface for dynamic loader ----
def get_display_capabilities() -> dict:
    w, h = get_inky_resolution()
    oinfo = orientation_info(w, h)
    colour = get_inky_colour_variant()
    return {
        "resolution": [oinfo.logical_width, oinfo.logical_height],
        "native_resolution": [w, h],
        "orientation": oinfo.name,
        "rotation_deg": oinfo.rotation_deg,
        "supported_formats": ["jpg", "jpeg", "png"],
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": bool(use_fake or inky is None),
        "backend": "inky",
        "color_variant": colour,
        "init_error": type(_inky_init_error).__name__ if _inky_init_error else None,
    }


def display_image(image_path: str) -> None:  # adapter
    show_on_inky(image_path)


def is_development_mode() -> bool:
    return use_fake or is_dev_mode()
