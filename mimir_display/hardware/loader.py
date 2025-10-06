"""Dynamic display backend loader.

Resolution order:
1. Explicit --backend CLI arg
2. DISPLAY_BACKEND env var
3. Autodetect known backends (hyperpixelsq, hdmi generic framebuffer, inky fallback)
4. Simulation fallback (logs only)

Environment override helpers:
    FORCE_INKY=1   -> force inky
    FORCE_HDMI=1   -> force hdmi (generic fb)
    FORCE_SIM=1    -> force simulation (handled in load)
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
    "inky": "mimir_display.hardware.eframe_inky",
    "simulation": "mimir_display.hardware.simulation",
    "rgbmatrix": "mimir_display.hardware.rgbmatrix_led",
    "hdmi": "mimir_display.hardware.hdmi",
}


def _import_backend(name: str) -> Any:
    mod_path = _KNOWN_MODULES.get(name)
    if not mod_path:
        raise ValueError(f"Unknown backend '{name}'")
    return importlib.import_module(mod_path)


def autodetect_backend() -> str:
    """Best-effort backend autodetection.

    Strategy:
      * Honor FORCE_INKY / FORCE_HDMI early.
      * If /dev/fb0 exists inspect geometry:
          - 720x720 (+16bpp) -> hyperpixelsq
          - anything else -> hdmi (generic framebuffer)
      * If no framebuffer -> inky (legacy default) to preserve prior behavior.
    """
    if os.environ.get("FORCE_INKY") == "1":
        return "inky"
    if os.environ.get("FORCE_HDMI") == "1":
        return "hdmi"

    fb_path = "/dev/fb0"
    if os.path.exists(fb_path):
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
                    pass
                if w == 720 and h == 720 and bpp in (16, 0):
                    return "hyperpixelsq"
                # Any other framebuffer geometry => treat as generic HDMI
                return "hdmi"
        except Exception:
            # On inspection error but fb present: prefer hdmi (generic) to avoid mislabeling
            return "hdmi"
    return "inky"


def load_backend(explicit: str | None = None):
    """Load a display backend.

    Fallback semantics (ordered):
      1. If ENVIRONMENT=development or FORCE_SIM=1 and no explicit non-sim backend -> simulation.
      2. Try explicit backend (unless 'auto').
      3. Autodetect (hyperpixelsq vs inky).
      4. On import failure: propagate error unless FALLBACK_SIM=1 (then simulation).
    """
    env_mode = (os.getenv("ENVIRONMENT") or "").strip().lower()
    force_sim = os.getenv("FORCE_SIM") == "1"
    allow_fallback = os.getenv("FALLBACK_SIM", "1") in ("1", "true", "yes")  # default allow

    name = explicit or os.environ.get("DISPLAY_BACKEND")
    if name == "auto":
        name = None

    # Development: prefer real hardware unless user explicitly picked simulation or forced sim
    if (env_mode in ("development", "dev") or force_sim) and (not name or name == "simulation"):
        name = "simulation"

    if not name:
        name = autodetect_backend()

    os.environ.setdefault("BACKEND", name)

    try:
        mod = _import_backend(name)
    except Exception as e:  # pragma: no cover - defensive
        if name == "simulation":
            # Simulation import itself failed – surface hard error.
            raise
        if allow_fallback:
            print(f"Backend '{name}' load failed: {e}; using simulation fallback")
            from mimir_display.hardware import simulation  # local import
            return simulation.make(original=name, init_error=e)
        # Fallback not allowed: re-raise to let caller crash (desired strict behavior)
        raise

    # Validate required attributes
    required = ["get_display_capabilities", "display_image", "is_development_mode"]
    missing = [attr for attr in required if not hasattr(mod, attr)]
    if missing:
        if os.getenv("FALLBACK_SIM", "1") in ("1", "true", "yes"):
            print(f"Backend '{name}' missing attributes {missing}; using simulation")
            from mimir_display.hardware import simulation
            return simulation.make(original=name, init_error=RuntimeError(f"missing {missing}"))
        raise RuntimeError(f"Backend '{name}' missing attributes: {missing}")
    return mod


def _simulation_backend(original: str, error: str | None = None):  # backward compat helper
    from mimir_display.hardware import simulation
    return simulation.make(original=original, init_error=RuntimeError(error) if error else None)


__all__ = [
    "DisplayBackend",
    "load_backend",
    "autodetect_backend",
]
