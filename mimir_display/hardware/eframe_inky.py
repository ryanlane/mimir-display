# hardware/eframe_inky.py

from PIL import Image
import os
import logging
import importlib.util
import traceback
import warnings
from datetime import datetime
from dotenv import load_dotenv


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
if not use_fake:
    try:
        from inky.auto import auto
        inky = auto(ask_user=False, verbose=False)  # Don't ask user in non-interactive mode
    except ImportError as ie:
        _inky_init_error = ie
        print("Warning: inky package not installed or failed to import, running in fake mode")
        use_fake = True
    except Exception as e:
        _inky_init_error = e
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
    if use_fake or inky is None:
        return os.getenv("FAKE_INKY_COLOR", "simulated")
    return getattr(inky, "colour", getattr(inky, "color", "unknown"))

def show_on_inky(imagepath):
    if use_fake or inky is None:
        logger.info("[DEV] Would display: %s", imagepath)
        return

    logger.info("Updating image: %s", imagepath)

    img = Image.open(imagepath)
    inky.set_image(img)
    inky.set_border(inky.BLACK)

    logger.info("Inky refresh started (~20–35s) …")
    inky.show()
    logger.info("Inky refresh complete")