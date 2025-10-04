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

from typing import Any, Tuple
import os

from .loader import load_backend

_backend_mod: Any | None = None


def _ensure_backend():
    global _backend_mod
    if _backend_mod is not None:
        return _backend_mod
    # Environment variable DISPLAY_BACKEND may be 'auto' – treat as None to force autodetect
    explicit = os.environ.get("DISPLAY_BACKEND")
    if explicit == "auto":  # normalize sentinel
        explicit = None
    _backend_mod = load_backend(explicit)
    return _backend_mod


def get_display_capabilities() -> dict:
    mod = _ensure_backend()
    try:
        return mod.get_display_capabilities()
    except Exception:  # pragma: no cover - defensive
        return {
            "resolution": [800, 480],
            "native_resolution": [800, 480],
            "supported_formats": ["jpg", "jpeg", "png"],
            "orientation": "landscape",
            "rotation_deg": 0,
            "refresh_rate_hz": 1,
            "redis_distribution": True,
            "content_claiming": True,
            "simulation_mode": True,
            "backend": "simulation(fallback)",
        }


def get_display_resolution() -> Tuple[int, int]:
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
	"is_development_mode",
	"get_display_capabilities",
	"HARDWARE_AVAILABLE",
]
