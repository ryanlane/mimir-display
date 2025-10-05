"""Dynamic display backend loader.

Resolution order:
1. Explicit --backend CLI arg
2. DISPLAY_BACKEND env var
3. Autodetect known backends (hyperpixelsq, inky)
4. Simulation fallback (logs only)
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Protocol


class DisplayBackend(Protocol):  # minimal protocol for type checkers
    def get_display_capabilities(self) -> dict: ...  # noqa: D401,E701 - succinct
    def display_image(self, image_path: str) -> None: ...
    def is_development_mode(self) -> bool: ...


_KNOWN_MODULES = {
    "hyperpixelsq": "mimir_display.hardware.hyperpixelsq",
    # The inky implementation lives in eframe_inky to preserve older naming elsewhere.
    "inky": "mimir_display.hardware.eframe_inky",
}


def _import_backend(name: str) -> Any:
    mod_path = _KNOWN_MODULES.get(name)
    if not mod_path:
        raise ValueError(f"Unknown backend '{name}'")
    return importlib.import_module(mod_path)


def autodetect_backend() -> str:
    """Best-effort backend autodetection.

    Strategy:
    1. If /dev/fb0 exists inspect sysfs for geometry to detect HyperPixel Square (720x720 @ 16bpp)
    2. FALLBACK to inky (historical default) when framebuffer not present or size mismatch

    Environment overrides:
    * FORCE_INKY=1  -> always choose inky (even if fb0 present)
    * FORCE_SIM=1   -> skip detection and force simulation (handled later when import fails)
    """
    if os.environ.get("FORCE_INKY") == "1":
        return "inky"

    fb_path = "/dev/fb0"
    if os.path.exists(fb_path):
        # HyperPixel Square characteristics: 720x720 logical resolution, 16bpp rgb565
        virt_size_path = "/sys/class/graphics/fb0/virtual_size"
        bpp_path = "/sys/class/graphics/fb0/bits_per_pixel"
        try:
            with open(virt_size_path, encoding="utf-8") as f:
                size_txt = f.read().strip()
            w_h = size_txt.split(",")
            if len(w_h) == 2:
                w, h = (int(w_h[0]), int(w_h[1]))
                bpp = 0
                try:
                    with open(bpp_path, encoding="utf-8") as f2:
                        bpp = int(f2.read().strip())
                except Exception:
                    pass  # non-fatal
                if w == 720 and h == 720 and bpp in (16, 0):  # tolerate unknown bpp
                    return "hyperpixelsq"
        except Exception:
            # If any inspection fails but fb0 exists, still assume HyperPixel unless FORCE_INKY
            return "hyperpixelsq"
    return "inky"  # default legacy


def load_backend(explicit: str | None = None):
    # Normalize 'auto' sentinel to trigger autodetection.
    name = explicit or os.environ.get("DISPLAY_BACKEND")
    if name == "auto":
        name = None
    if not name:
        name = autodetect_backend()
    else:
        # Record explicit request so backend heuristics (_want_inky_backend) can see it
        # without forcing users to set multiple different env vars.
        os.environ.setdefault("BACKEND", name)
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
