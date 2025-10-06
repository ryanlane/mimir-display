"""Generic HDMI display backend using a fullscreen window.

Goals:
  * Provide a "just works" backend for any Raspberry Pi (or dev workstation)
    connected to an HDMI monitor without relying on framebuffer mmap hacks.
  * Keep zero mandatory extra deps for headless installs: we try to import
    `pygame` (preferred for speed) else fall back to a very small Pillow based
    preview in a blocking window via `tkinter` (as an absolute last resort).
  * Expose capabilities similar to other backends: logical resolution respects
    `DISPLAY_ORIENTATION` (handled by orientation utility), color, supported
    formats.

Environment overrides:
  HDMI_RESOLUTION=WxH        Force a logical/native resolution (e.g. 1920x1080)
  HDMI_WINDOWED=1            Do not request fullscreen (useful for development)
  HDMI_SCALE_MODE=fit|fill   How to scale source images (default: fit)
  HDMI_BG_COLOR=#RRGGBB      Background color when aspect doesn't match (fit)

Notes:
  * We intentionally don't attempt to auto-detect the desktop size via platform
    APIs when pygame is missing; tkinter introspection varies across window
    managers. Users can set HDMI_RESOLUTION if auto detection fails.
  * Pygame path: create a single window, keep it open for the life of the
    process. Each display_image() blits & flips.
  * Tkinter fallback: open a window per image (simpler) – acceptable because
    this path is rarely used in production. We still cache the root if possible.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from PIL import Image
from mimir_display.utils.orientation import orientation_info

# Attempt pygame import (optional dependency)
try:  # pragma: no cover - exercised only when pygame installed
    import pygame  # type: ignore
    _HAS_PYGAME = True
except Exception:  # pragma: no cover - typical on minimal installs
    pygame = None  # type: ignore
    _HAS_PYGAME = False

try:  # Tk fallback (only if pygame absent)
    if not _HAS_PYGAME:
        import tkinter as tk  # type: ignore
except Exception:  # pragma: no cover
    tk = None  # type: ignore


@dataclass
class _State:
    # Native (desktop) dimensions selected for the window / fullscreen surface
    width: int
    height: int
    # Logical (post-orientation) dimensions exposed to rest of system
    logical_width: int
    logical_height: int
    windowed: bool
    scale_mode: str
    bg_color: tuple[int, int, int]
    orientation: str
    rotation_deg: int
    detected: bool  # whether resolution was auto-detected (vs env/default)


_state: _State | None = None
_pygame_screen = None  # type: ignore
_tk_root = None  # type: ignore


_DEF_BG = (0, 0, 0)


def _parse_resolution() -> tuple[tuple[int, int], bool]:
    """Return (resolution, detected_flag).

    Priority:
      1. Explicit HDMI_RESOLUTION env (detected_flag=False)
      2. Pygame desktop display current_w/current_h if pygame present (detected_flag=True)
      3. Fallback default 1280x720 (detected_flag=False)
    """
    env = os.getenv("HDMI_RESOLUTION")
    if env and "x" in env:
        try:
            w_s, h_s = env.lower().split("x", 1)
            return (int(w_s), int(h_s)), False
        except Exception:
            pass
    # Attempt auto-detect via pygame (if already imported successfully)
    if _HAS_PYGAME:
        try:  # pragma: no cover - depends on runtime video subsystem
            if not pygame.get_init():
                pygame.display.init()
            info = pygame.display.Info()
            if getattr(info, "current_w", 0) and getattr(info, "current_h", 0):
                return (int(info.current_w), int(info.current_h)), True
        except Exception:
            pass
    return (1280, 720), False


def _parse_bg() -> tuple[int, int, int]:
    val = os.getenv("HDMI_BG_COLOR", "#000000").strip()
    if val.startswith("#") and len(val) == 7:
        try:
            r = int(val[1:3], 16)
            g = int(val[3:5], 16)
            b = int(val[5:7], 16)
            return r, g, b
        except Exception:
            return _DEF_BG
    return _DEF_BG


def _init_state() -> _State:
    global _state, _pygame_screen
    if _state is not None:
        return _state

    (base_w, base_h), detected = _parse_resolution()
    oinfo = orientation_info(base_w, base_h)
    scale_mode = os.getenv("HDMI_SCALE_MODE", "fit").lower()
    if scale_mode not in {"fit", "fill"}:
        scale_mode = "fit"
    windowed = os.getenv("HDMI_WINDOWED") == "1"
    bg_color = _parse_bg()

    if _HAS_PYGAME:  # pragma: no cover - requires pygame in test env
        # Basic init (avoid re-init noise)
        if not pygame.get_init():
            pygame.display.init()
        flags = 0
        if not windowed:
            flags |= pygame.FULLSCREEN
        _pygame_screen = pygame.display.set_mode((base_w, base_h), flags)
        pygame.display.set_caption("Mimir HDMI Display")

    _state = _State(
        width=base_w,
        height=base_h,
        logical_width=oinfo.logical_width,
        logical_height=oinfo.logical_height,
        windowed=windowed,
        scale_mode=scale_mode,
        bg_color=bg_color,
        orientation=oinfo.name,
        rotation_deg=oinfo.rotation_deg,
        detected=detected,
    )
    return _state


def _scale_image(img: Image.Image, target: tuple[int, int], mode: str) -> Image.Image:
    if img.size == target:
        return img
    tw, th = target
    if mode == "fill":
        return img.resize(target, Image.LANCZOS)
    # fit mode with letterbox
    iw, ih = img.size
    ratio = min(tw / iw, th / ih)
    new_size = (max(1, int(iw * ratio)), max(1, int(ih * ratio)))
    return img.resize(new_size, Image.LANCZOS)


def get_display_capabilities() -> dict:
    st = _init_state()
    # Logical dims may swap for portrait, orientation_info already handled.
    return {
        # Logical (orientation-adjusted) resolution exposed to upstream services
        "resolution": [st.logical_width, st.logical_height],
        # Native desktop/window resolution (pre-orientation)
        "native_resolution": [st.width, st.height],
        "orientation": st.orientation,
        "rotation_deg": st.rotation_deg,
        "supported_formats": ["jpg", "jpeg", "png"],
        "redis_distribution": True,
        "content_claiming": True,
        "simulation_mode": False,  # always a real window (even if off-screen)
        "color": True,
        "pixel_format": "RGB888",
        "backend": "hdmi",
        "scale_mode": st.scale_mode,
        "windowed": st.windowed,
        "bg_color": st.bg_color,
        "driver": "pygame" if _HAS_PYGAME else ("tkinter" if tk else "none"),
        "auto_detected_resolution": st.detected,
    }


def display_image(image_path: str) -> None:  # pragma: no cover - UI side effects
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)
    st = _init_state()
    img = Image.open(image_path).convert("RGB")
    # Apply rotation for orientation (we rotate image so window stays base_w x base_h)
    if st.rotation_deg:
        img = img.rotate(st.rotation_deg, expand=True)
    scaled = _scale_image(img, (st.width, st.height), st.scale_mode)

    if _HAS_PYGAME:
        surf = pygame.image.fromstring(scaled.tobytes(), scaled.size, "RGB")
        # Letterbox if fit and aspect mismatch
        if st.scale_mode == "fit" and scaled.size != (st.width, st.height):
            _pygame_screen.fill(st.bg_color)  # type: ignore
            x = (st.width - scaled.width) // 2
            y = (st.height - scaled.height) // 2
            _pygame_screen.blit(surf, (x, y))  # type: ignore
        else:
            _pygame_screen.blit(surf, (0, 0))  # type: ignore
        pygame.display.flip()
        for event in pygame.event.get():  # pump queue to keep OS happy
            if event.type == pygame.QUIT:
                pass
        return

    # Tk fallback ---------------------------------------------------------
    if tk is None:
        print(f"[hdmi] (no pygame/tk) would display {image_path}")
        return
    global _tk_root
    if _tk_root is None:
        _tk_root = tk.Tk()
        if not st.windowed:
            _tk_root.attributes("-fullscreen", True)
        else:
            _tk_root.geometry(f"{st.width}x{st.height}")
        _tk_root.title("Mimir HDMI Display")
    from PIL import ImageTk  # late import

    tk_img = ImageTk.PhotoImage(scaled)
    lbl = tk.Label(_tk_root, image=tk_img, bg=f"#{st.bg_color[0]:02x}{st.bg_color[1]:02x}{st.bg_color[2]:02x}")
    lbl.image = tk_img  # keep reference
    lbl.pack(expand=True, fill="both")
    _tk_root.update_idletasks()
    _tk_root.update()


def is_development_mode() -> bool:
    # We treat HDMI window as production-capable; not a simulation.
    return False


__all__ = [
    "get_display_capabilities",
    "display_image",
    "is_development_mode",
]
