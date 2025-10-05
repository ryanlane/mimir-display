"""Simulation display backend.

Used when ENVIRONMENT=development (opt-in dev mode) or as a last-resort
fallback when a real hardware backend cannot be initialized.

The loader decides when to choose this module; hardware modules should raise
on hard failures so the loader can escalate instead of silently simulating.
"""
from __future__ import annotations

from dataclasses import dataclass

@dataclass
class _SimCapabilities:
    width: int = 400
    height: int = 300
    orientation: str = "landscape"
    rotation_deg: int = 0


class SimulationBackend:
    def __init__(self, original: str | None = None, init_error: str | None = None):
        self._original = original or "unknown"
        self._init_error = init_error
        self._caps = _SimCapabilities()

    # Interface -------------------------------------------------------------
    def get_display_capabilities(self) -> dict:
        return {
            "resolution": [self._caps.width, self._caps.height],
            "native_resolution": [self._caps.width, self._caps.height],
            "resolution_source": "simulation",
            "orientation": self._caps.orientation,
            "rotation_deg": self._caps.rotation_deg,
            "supported_formats": ["jpg", "jpeg", "png"],
            "redis_distribution": True,
            "content_claiming": True,
            "simulation_mode": True,
            "backend": f"simulation({self._original})",
            "color_variant": "simulated",
            "init_error": self._init_error,
        }

    def display_image(self, image_path: str) -> None:  # pragma: no cover - simple
        print(f"[SIM] Would display {image_path} (original backend={self._original})")

    def is_development_mode(self) -> bool:
        return True


def make(original: str | None = None, init_error: Exception | None = None) -> SimulationBackend:
    return SimulationBackend(original=original, init_error=type(init_error).__name__ if init_error else None)

__all__ = ["make", "SimulationBackend"]
