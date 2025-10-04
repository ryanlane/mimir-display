"""Dynamic display backend loader.

Resolution order:
1. Explicit --backend CLI arg
2. DISPLAY_BACKEND env var
3. Autodetect known backends (hyperpixelsq, inky)
4. Simulation fallback (logs only)
"""
from __future__ import annotations

import os
import importlib
from typing import Any, Protocol


class DisplayBackend(Protocol):  # minimal protocol for type checkers
    def get_display_capabilities(self) -> dict: ...  # noqa: D401,E701 - succinct
    def display_image(self, image_path: str) -> None: ...
    def is_development_mode(self) -> bool: ...


_KNOWN_MODULES = {
    "hyperpixelsq": "mimir_display.hardware.hyperpixelsq",
    "inky": "mimir_display.hardware.inky",
}


def _import_backend(name: str) -> Any:
    mod_path = _KNOWN_MODULES.get(name)
    if not mod_path:
        raise ValueError(f"Unknown backend '{name}'")
    return importlib.import_module(mod_path)


def autodetect_backend() -> str:
    # HyperPixel detection: /dev/fb0 + optional env HYPERPIXEL=1
    if os.path.exists("/dev/fb0") and os.environ.get("FORCE_INKY") != "1":
        return "hyperpixelsq"
    return "inky"  # default legacy


def load_backend(explicit: str | None = None):
    name = explicit or os.environ.get("DISPLAY_BACKEND")
    if not name:
        name = autodetect_backend()
    try:
        mod = _import_backend(name)
    except Exception as e:  # pragma: no cover - defensive
        print(f"Backend '{name}' load failed: {e}; falling back to simulation")
        return _simulation_backend(name, error=str(e))
    # Validate required attributes
    required = ["get_display_capabilities", "display_image", "is_development_mode"]
    for attr in required:
        if not hasattr(mod, attr):
            print(f"Backend '{name}' missing attribute {attr}; using simulation")
            return _simulation_backend(name, error=f"missing {attr}")
    return mod


def _simulation_backend(original: str, error: str | None = None):  # pragma: no cover - simple
    class SimBackend:
        def get_display_capabilities(self) -> dict:
            return {
                "resolution": [400, 300],
                "native_resolution": [400, 300],
                "orientation": "landscape",
                "rotation_deg": 0,
                "supported_formats": ["jpg", "jpeg", "png"],
                "redis_distribution": True,
                "content_claiming": True,
                "simulation_mode": True,
                "backend": f"simulation({original})",
                "load_error": error,
            }

        def display_image(self, image_path: str) -> None:
            print(f"SIMULATION: would display {image_path} (original backend={original})")

        def is_development_mode(self) -> bool:
            return True

    return SimBackend()


__all__ = [
    "DisplayBackend",
    "load_backend",
    "autodetect_backend",
]
