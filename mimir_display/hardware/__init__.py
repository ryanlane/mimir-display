"""Unified hardware abstraction layer.

Historically this package re-exported only the Inky implementation. It now
selects a backend dynamically (hyperpixelsq, inky, or simulation fallback)
using :func:`mimir_display.hardware.loader.load_backend`.

Public API (stable):
  get_display_capabilities() -> dict
  get_display_resolution()   -> (w, h)
  display_image(path: str)   -> None (best-effort)
  is_development_mode()      -> bool
  HARDWARE_AVAILABLE         -> bool (derived from backend)
"""

from __future__ import annotations

import os
import traceback
from typing import Any

from .loader import load_backend

_backend_mod: Any | None = None
_backend_key: str | None = None


def _ensure_backend():
    global _backend_mod, _backend_key
    # Environment variable DISPLAY_BACKEND may be 'auto' – treat as None to force autodetect
    explicit = os.environ.get("DISPLAY_BACKEND")
    if explicit == "auto":  # normalize sentinel
        explicit = None
    requested_key = explicit or "<auto>"
    if _backend_mod is not None and _backend_key == requested_key:
        return _backend_mod
    _backend_mod = load_backend(explicit)
    _backend_key = requested_key
    return _backend_mod


def get_display_capabilities() -> dict:
    mod = _ensure_backend()
    try:
        return mod.get_display_capabilities()
    except Exception as e:  # pragma: no cover - defensive
        debug = os.getenv("DISPLAY_CAPS_DEBUG") == "1"
        if debug:
            print("[hardware] capability retrieval failed:", e)
            traceback.print_exc()
        return {
            "resolution": [800, 480],
            "native_resolution": [800, 480],
            "supported_formats": ["jpg", "jpeg", "png"],
            "supports_animation": False,
            "orientation": "landscape",
            "rotation_deg": 0,
            "refresh_rate_hz": 1,
            "redis_distribution": True,
            "content_claiming": True,
            "simulation_mode": True,
            "backend": "simulation(fallback)",
            "cap_error": type(e).__name__,
        }


def get_display_resolution() -> tuple[int, int]:
    caps = get_display_capabilities()
    res = caps.get("resolution") or caps.get("native_resolution") or [800, 480]
    try:
        return int(res[0]), int(res[1])
    except Exception:  # pragma: no cover
        return (800, 480)


def display_image(image_path: str) -> None:
    mod = _ensure_backend()
    if hasattr(mod, "display_image"):
        try:
            mod.display_image(image_path)
            return
        except Exception:  # pragma: no cover - log suppressed here
            pass
    # Fallback noop / debug print
    print(f"SIMULATION: would display {image_path}")


def supports_pil_playback() -> bool:
    """True when the active backend can take PIL frames directly —
    the prerequisite for animation playback (no per-frame file I/O)."""
    mod = _ensure_backend()
    return callable(getattr(mod, "display_pil", None))


def display_pil(img) -> None:
    """Push one PIL frame to the backend. Raises if unsupported —
    callers must check supports_pil_playback() first."""
    mod = _ensure_backend()
    fn = getattr(mod, "display_pil", None)
    if fn is None:
        raise NotImplementedError(f"backend has no display_pil: {_backend_key}")
    fn(img)


def supports_frame_bytes() -> bool:
    """True when the backend can pre-convert frames to raw framebuffer
    bytes and write them directly — the fastest playback path."""
    mod = _ensure_backend()
    return (callable(getattr(mod, "prepare_frame", None))
            and callable(getattr(mod, "display_frame_bytes", None)))


def prepare_frame(img) -> bytes:
    """Convert a PIL frame to native framebuffer bytes (backend-specific)."""
    return _ensure_backend().prepare_frame(img)


def display_frame_bytes(data: bytes) -> None:
    """Write pre-converted framebuffer bytes to the panel."""
    _ensure_backend().display_frame_bytes(data)


def is_development_mode() -> bool:
    mod = _ensure_backend()
    if hasattr(mod, "is_development_mode"):
        try:
            return bool(mod.is_development_mode())
        except Exception:  # pragma: no cover
            return True
    return True


def _hardware_available() -> bool:
    caps = get_display_capabilities()
    return not caps.get("simulation_mode", True)


HARDWARE_AVAILABLE = _hardware_available()

__all__ = [
    "get_display_resolution",
    "display_image",
    "display_pil",
    "supports_pil_playback",
    "supports_frame_bytes",
    "prepare_frame",
    "display_frame_bytes",
    "is_development_mode",
    "get_display_capabilities",
    "HARDWARE_AVAILABLE",
]
