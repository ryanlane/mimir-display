"""Orientation utilities.

Provides helpers to interpret the DISPLAY_ORIENTATION environment variable
and compute rotation plus logical resolution to report upstream.

Environment variable: DISPLAY_ORIENTATION
Accepted values (case-insensitive):
    landscape       - (default) no rotation applied
    landscape_up    - alias for landscape
    landscape_down  - rotate 180°
    landscape_inverted - alias for landscape_down
    portrait_left   - panel physically rotated CCW 90° (top was original left)
    portrait_right  - panel physically rotated CW 90° (top was original right)
    portrait_up     - alias for portrait_right
    portrait_down   - alias for portrait_left
    square          - explicit square semantic (no rotation, width==height)

Auto-detection fallback (when DISPLAY_ORIENTATION is unset):
    * If native_w == native_h -> "square" (square semantic)
    * Else if native_h > native_w -> infer portrait_right (arbitrary but consistent choice)
    * Else -> landscape

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

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class OrientationInfo:
    name: str               # normalized orientation name
    rotation_deg: int       # degrees to rotate image clockwise before display
    logical_width: int      # width reported to rest of system
    logical_height: int     # height reported to rest of system


def parse_orientation(raw: str | None) -> str:
    """Normalize raw env orientation value.

    Returns 'landscape' for any unrecognized value or None.
    """
    if not raw:
        return "landscape"
    val = raw.strip().lower()
    aliases = {
        "landscape": "landscape",
        "landscape_up": "landscape",
        "landscape_down": "landscape_inverted",
        "landscape_inverted": "landscape_inverted",
        "portrait": "portrait_right",
        "portrait_up": "portrait_right",
        "portrait_right": "portrait_right",
        "portrait_down": "portrait_left",
        "portrait_left": "portrait_left",
        "square": "square",
    }
    if val in aliases:
        return aliases[val]
    return "landscape"


def orientation_info(native_w: int, native_h: int, env_value: str | None = None) -> OrientationInfo:
    """Return orientation info given the panel's native (landscape) resolution.

    Resolution parameters (native_w, native_h) are assumed in the panel's
    physical landscape ordering. The logical dimensions returned may swap
    when a portrait orientation is selected.

    Auto-detection: If env is unset/empty we infer a reasonable default:
    * native_w == native_h -> square
        * native_h > native_w  -> portrait_right (arbitrary but stable)
        * else -> landscape
    """
    raw = env_value if env_value is not None else os.getenv("DISPLAY_ORIENTATION")
    if raw:
        name = parse_orientation(raw)
    else:
        # Auto inference path
        if native_w == native_h:
            name = "square"
        elif native_h > native_w:
            # Choose portrait_right as a conventional mapping (CW rotation)
            name = "portrait_right"
        else:
            name = "landscape"

    if name == "square":
        return OrientationInfo("square", 0, native_w, native_h)
    if name == "landscape_inverted":
        return OrientationInfo(name, 180, native_w, native_h)
    if name == "portrait_left":
        return OrientationInfo(name, 90, native_h, native_w)
    if name == "portrait_right":
        return OrientationInfo(name, 270, native_h, native_w)
    return OrientationInfo("landscape", 0, native_w, native_h)


def should_swap(name: str) -> bool:
    return name.startswith("portrait")
