"""Animation playback for LCD/OLED framebuffer backends.

Plays animated WebP/GIF content (e.g. the Generative Art source's animated
output) by pushing pre-fitted PIL frames through the backend's display_pil
fast path on a timed loop. E-ink backends never get a player — they show
the first frame via the static path.

Design notes:
  * All frames are decoded and fitted ONCE up front. Playback then only
    converts + writes — no per-frame decode, resize, or file I/O.
  * The loop honors per-frame durations from the file, clamped by
    ANIMATION_MAX_FPS so a pathological file can't peg the CPU.
  * ANIMATION_MAX_FRAMES caps decode memory (24 frames @ 800x480 RGB is
    ~27 MB; the cap protects small boards from oversized loops).
  * stop() is synchronous: after it returns no further frames are written,
    so a new static render can't be raced and overwritten by a stale frame.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable

from PIL import Image, ImageSequence

DEFAULT_MAX_FRAMES = int(os.environ.get("ANIMATION_MAX_FRAMES", "90"))
DEFAULT_MAX_FPS = float(os.environ.get("ANIMATION_MAX_FPS", "15"))
_DEFAULT_FRAME_MS = 100


def load_animation_frames(
    img: Image.Image,
    fit: Callable[[Image.Image], Image.Image],
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> tuple[list[Image.Image], list[int]]:
    """Decode an animated image into fitted RGB frames + per-frame ms.

    ``fit`` is the same fit/rotate pipeline the static path uses, so an
    animation is framed exactly like its static counterpart.
    """
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        duration = frame.info.get("duration", _DEFAULT_FRAME_MS)
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = _DEFAULT_FRAME_MS
        durations.append(max(10, duration))
        frames.append(fit(frame.convert("RGB")))
        if len(frames) >= max_frames:
            break
    return frames, durations


class AnimationPlayer:
    """Loops pre-fitted frames through a frame writer on its own thread.

    Frames are opaque to the player — PIL images for backends that take
    display_pil, or pre-converted framebuffer bytes for backends with the
    prepare_frame/display_frame_bytes fast path (preferred: the per-frame
    pixel conversion is far too slow to run inside the loop)."""

    def __init__(
        self,
        frames: list,
        durations_ms: list[int],
        write_frame: Callable[[object], None],
        logger,
        max_fps: float = DEFAULT_MAX_FPS,
    ):
        if not frames:
            raise ValueError("AnimationPlayer needs at least one frame")
        self._frames = frames
        self._durations = [d / 1000.0 for d in durations_ms]
        while len(self._durations) < len(frames):
            self._durations.append(_DEFAULT_FRAME_MS / 1000.0)
        self._min_frame_time = 1.0 / max(1.0, max_fps)
        self._write_frame = write_frame
        self._logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mimir-animation-player", daemon=True)
        self._thread.start()
        self._logger.info(
            "AnimationPlayer: started (%d frames, ~%.1fs loop)",
            len(self._frames), sum(self._durations))

    def stop(self, timeout: float = 3.0) -> None:
        """Signal the loop to stop and wait for it — synchronous so a
        follow-up static render can't be clobbered by a late frame."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def _run(self) -> None:
        consecutive_errors = 0
        while not self._stop.is_set():
            for frame, duration in zip(self._frames, self._durations):
                if self._stop.is_set():
                    return
                started = time.monotonic()
                try:
                    self._write_frame(frame)
                    consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001 — keep the loop alive
                    consecutive_errors += 1
                    if consecutive_errors == 1:
                        self._logger.warning("AnimationPlayer: frame write failed: %s", exc)
                    if consecutive_errors >= 5:
                        self._logger.error(
                            "AnimationPlayer: %d consecutive write failures — stopping",
                            consecutive_errors)
                        return
                # Honor the frame duration, minus time spent writing, and
                # never faster than the fps clamp.
                elapsed = time.monotonic() - started
                wait = max(duration, self._min_frame_time) - elapsed
                if wait > 0 and self._stop.wait(wait):
                    return
