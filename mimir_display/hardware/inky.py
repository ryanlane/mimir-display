"""
hardware/inky.py

Hardware abstraction for Inky e-paper displays.

This module provides a clean interface to the Inky display hardware,
abstracting away the low-level details and providing easy-to-use
functions for display operations.
"""

import os
import sys
import traceback
import logging
from typing import Tuple
from mimir_display.utils.orientation import orientation_info

# Add current directory to path for eframe_inky import
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

logger = logging.getLogger(__name__)

try:
    from mimir_display.hardware.eframe_inky import get_inky_resolution, show_on_inky, is_dev_mode  # type: ignore
    HARDWARE_AVAILABLE = True
except ImportError as e:
    HARDWARE_AVAILABLE = False
    # Provide richer diagnostics to help end users understand why we fell back.
    msg = [
        "[inky] WARNING: Could not import eframe_inky driver; running in simulation mode.",
        f"ImportError: {e}",
        "Environment snapshot (key vars):",
        f"  ENVIRONMENT={os.getenv('ENVIRONMENT')}",
        f"  DEV_MODE={os.getenv('DEV_MODE')}",
        f"  NO_HARDWARE={os.getenv('NO_HARDWARE')}",
        f"  BACKEND={os.getenv('BACKEND')}",
        f"  DISPLAY_BACKEND={os.getenv('DISPLAY_BACKEND')}",
        f"  DISPLAY_ORIENTATION={os.getenv('DISPLAY_ORIENTATION')}",
        "Traceback (most recent call last):",
        ''.join(traceback.format_exception_only(type(e), e)).strip(),
        "Hint: Ensure 'inky' package is installed in this venv and that FORCE_INKY_HARDWARE/BACKEND=inky is set if auto-detection skipped.",
    ]
    for line in msg:
        try:
            print(line)
        except Exception:
            pass

    # Mock functions for development/testing
    def get_inky_resolution():
        # Keep legacy fallback resolution but log once via logger (if configured)
        if logger.handlers:
            logger.debug("[inky] Using simulation resolution (400x300)")
        return (400, 300)

    def show_on_inky(image_path):
        print(f"[inky] SIMULATION: Would display image: {image_path}")

    def is_dev_mode():  # pragma: no cover - simple shim
        return True


def get_display_resolution() -> Tuple[int, int]:
    """
    Get the resolution of the connected Inky display.
    
    Returns:
        Tuple of (width, height) in pixels
    """
    return get_inky_resolution()


def display_image(image_path: str):
    """
    Display an image on the Inky display.
    
    Args:
        image_path: Path to the image file to display
        
    Raises:
        Exception: If display operation fails
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")
    
    show_on_inky(image_path)


def is_development_mode() -> bool:
    """
    Check if running in development mode.
    
    Returns:
        True if in development mode
    """
    return is_dev_mode()


def get_display_capabilities() -> dict:
    """Get complete display capabilities information with orientation handling.

    Reads DISPLAY_ORIENTATION and, for portrait modes, swaps logical width/height
    while retaining original native (landscape) order internally.
    """
    native_w, native_h = get_display_resolution()
    oinfo = orientation_info(native_w, native_h)

    capabilities = {
        "resolution": [oinfo.logical_width, oinfo.logical_height],  # logical (may be swapped)
        "native_resolution": [native_w, native_h],                  # always hardware landscape order
        "supported_formats": ["jpg", "jpeg", "png"],
        "orientation": oinfo.name,
        "rotation_deg": oinfo.rotation_deg,
        "refresh_rate_hz": 1,
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": not HARDWARE_AVAILABLE,
    }
    return capabilities
