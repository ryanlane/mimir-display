"""Orientation utilities.

Provides helpers to interpret the DISPLAY_ORIENTATION environment variable
and compute rotation plus logical resolution to report upstream.

Environment variable: DISPLAY_ORIENTATION
Accepted values (case-insensitive):
    landscape       - (default) no rotation applied
    portrait_left   - panel physically rotated CCW 90° (top was original left)
    portrait_right  - panel physically rotated CW 90° (top was original right)

Auto-detection fallback (when DISPLAY_ORIENTATION is unset):
    * If native_w == native_h -> "landscape" (square panel, treat as landscape coordinate space)
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

from dataclasses import dataclass
import os


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
    if val in ("landscape", "portrait_left", "portrait_right"):
        return val
    return "landscape"


def orientation_info(native_w: int, native_h: int, env_value: str | None = None) -> OrientationInfo:
    """Return orientation info given the panel's native (landscape) resolution.

    Resolution parameters (native_w, native_h) are assumed in the panel's
    physical landscape ordering. The logical dimensions returned may swap
    when a portrait orientation is selected.

    Auto-detection: If env is unset/empty we infer a reasonable default:
        * native_w == native_h -> landscape (square treated as landscape)
        * native_h > native_w  -> portrait_right (arbitrary but stable)
        * else -> landscape
    """
    raw = env_value if env_value is not None else os.getenv("DISPLAY_ORIENTATION")
    if raw:
        name = parse_orientation(raw)
    else:
        # Auto inference path
        if native_w == native_h:
            name = "landscape"
        elif native_h > native_w:
            # Choose portrait_right as a conventional mapping (CW rotation)
            name = "portrait_right"
        else:
            name = "landscape"

    if name == "portrait_left":
        return OrientationInfo(name, 90, native_h, native_w)
    if name == "portrait_right":
        return OrientationInfo(name, 270, native_h, native_w)
    return OrientationInfo("landscape", 0, native_w, native_h)


def should_swap(name: str) -> bool:
    return name.startswith("portrait")
