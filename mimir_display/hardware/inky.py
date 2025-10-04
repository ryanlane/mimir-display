"""
hardware/inky.py

Hardware abstraction for Inky e-paper displays.

This module provides a clean interface to the Inky display hardware,
abstracting away the low-level details and providing easy-to-use
functions for display operations.
"""

import os
import sys
from typing import Tuple
from mimir_display.utils.orientation import orientation_info

# Add current directory to path for eframe_inky import
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from mimir_display.hardware.eframe_inky import get_inky_resolution, show_on_inky, is_dev_mode
    HARDWARE_AVAILABLE = True
except ImportError as e:
    print(f"WARNING: Hardware not available: {e}")
    print("Running in simulation mode.")
    HARDWARE_AVAILABLE = False
    
    # Mock functions for development/testing
    def get_inky_resolution():
        return (400, 300)  # Default resolution
    
    def show_on_inky(image_path):
        print(f"SIMULATION: Would display image: {image_path}")
    
    def is_dev_mode():
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
