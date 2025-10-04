"""Orientation utilities.

Provides helpers to interpret the DISPLAY_ORIENTATION environment variable
and compute rotation plus logical resolution to report upstream.

Environment variable: DISPLAY_ORIENTATION
Accepted values (case-insensitive):
  landscape (default)
  portrait_left   -> panel physically rotated CCW 90° (top was original left).
  portrait_right  -> panel physically rotated CW 90° (top was original right).

Rotation semantics:
  We rotate the loaded image BEFORE sending to hardware so the hardware
  always receives a landscape-oriented bitmap matching the native panel
  coordinate system. That means:
    portrait_left  : rotate image 270° (or -90°) so content appears upright.
    portrait_right : rotate image 90° so content appears upright.
    landscape      : no rotation.

Logical resolution:
  For portrait_* orientations we swap width/height when reporting capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class OrientationInfo:
    name: str               # normalized orientation name
    rotation_deg: int       # degrees to rotate image clockwise before display
    logical_width: int      # width reported to rest of system
    logical_height: int     # height reported to rest of system


def parse_orientation(raw: str | None) -> str:
    if not raw:
        return "landscape"
    val = raw.strip().lower()
    if val in ("landscape", "portrait_left", "portrait_right"):
        return val
    return "landscape"


def orientation_info(native_w: int, native_h: int, env_value: str | None = None) -> OrientationInfo:
    """Return orientation info given the panel's native (landscape) resolution.

    Args:
        native_w: Native landscape width from hardware.
        native_h: Native landscape height from hardware.
        env_value: Optional override of orientation env value.

    Returns:
        OrientationInfo with rotation + logical resolution.
    """
    name = parse_orientation(env_value or os.getenv("DISPLAY_ORIENTATION"))

    if name == "portrait_left":
        # Physically rotated CCW -> need to rotate content +90 CW (i.e., 90 deg)
        return OrientationInfo(name, 90, native_h, native_w)
    if name == "portrait_right":
        # Physically rotated CW -> rotate content +270 CW (i.e., -90 deg)
        return OrientationInfo(name, 270, native_h, native_w)

    # Landscape default
    return OrientationInfo("landscape", 0, native_w, native_h)


def should_swap(name: str) -> bool:
    return name.startswith("portrait")
