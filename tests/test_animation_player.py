"""AnimationPlayer + DisplayManager playback behavior."""
import logging
import time

import pytest
from PIL import Image

from mimir_display.content.animation import AnimationPlayer, load_animation_frames

LOGGER = logging.getLogger("test")


def _frames(n=3, size=(40, 24)):
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    return [Image.new("RGB", size, colors[i % len(colors)]) for i in range(n)]


def _make_animated_webp(path, n=3):
    frames = _frames(n)
    frames[0].save(path, format="WEBP", save_all=True, append_images=frames[1:],
                   duration=30, loop=0)


class TestAnimationPlayer:
    def test_loops_frames_in_order(self):
        written = []
        player = AnimationPlayer(_frames(3), [10, 10, 10],
                                 lambda f: written.append(f.getpixel((0, 0))),
                                 LOGGER, max_fps=100)
        player.start()
        time.sleep(0.25)
        player.stop()
        assert len(written) >= 6, "should have looped at least twice"
        # Cycle order holds: red, green, blue, red, green, blue...
        expected = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
        for i, px in enumerate(written[:6]):
            assert px == expected[i % 3]

    def test_stop_is_synchronous(self):
        written = []
        player = AnimationPlayer(_frames(2), [10, 10],
                                 lambda f: written.append(1), LOGGER, max_fps=100)
        player.start()
        time.sleep(0.1)
        player.stop()
        count_at_stop = len(written)
        time.sleep(0.15)
        assert len(written) == count_at_stop, "no frames may be written after stop()"
        assert not player.running

    def test_fps_clamp_slows_pathological_durations(self):
        written = []
        player = AnimationPlayer(_frames(2), [1, 1],  # 1ms frames → 1000fps requested
                                 lambda f: written.append(1), LOGGER, max_fps=20)
        player.start()
        time.sleep(0.5)
        player.stop()
        # At a 20fps clamp, half a second fits ~10 frames (allow jitter headroom).
        assert len(written) <= 15

    def test_gives_up_after_repeated_write_failures(self):
        def bad_writer(_):
            raise RuntimeError("fb gone")
        player = AnimationPlayer(_frames(2), [5, 5], bad_writer, LOGGER, max_fps=100)
        player.start()
        time.sleep(0.3)
        assert not player.running, "player must stop itself after consecutive failures"

    def test_requires_frames(self):
        with pytest.raises(ValueError):
            AnimationPlayer([], [], lambda f: None, LOGGER)


class TestLoadAnimationFrames:
    def test_decodes_and_fits_all_frames(self, tmp_path):
        p = tmp_path / "a.webp"
        _make_animated_webp(p, n=4)
        img = Image.open(p)
        fitted_sizes = []

        def fit(frame):
            fitted_sizes.append(frame.size)
            return frame.resize((20, 12))

        frames, durations = load_animation_frames(img, fit)
        assert len(frames) == 4
        assert all(f.size == (20, 12) for f in frames)
        assert all(d >= 10 for d in durations)

    def test_frame_cap(self, tmp_path):
        p = tmp_path / "b.webp"
        _make_animated_webp(p, n=6)
        frames, _ = load_animation_frames(Image.open(p), lambda f: f, max_frames=3)
        assert len(frames) == 3


class TestDisplayManagerPlayback:
    def _manager(self, tmp_path):
        from mimir_display.content.display import DisplayManager
        return DisplayManager(
            capabilities={"resolution": [40, 24], "orientation": "landscape"},
            cache_dir=str(tmp_path),
            logger=LOGGER,
        )

    @staticmethod
    def _wait_for_player(manager, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            player = manager._anim_player
            if player is not None and player.running:
                return player
            time.sleep(0.02)
        raise AssertionError("animation player did not start in time")

    def test_animated_file_plays_when_backend_supports_pil(self, tmp_path, monkeypatch):
        import mimir_display.hardware as hw
        written = []
        monkeypatch.setattr(hw, "supports_pil_playback", lambda: True, raising=False)
        monkeypatch.setattr(hw, "supports_frame_bytes", lambda: False, raising=False)
        monkeypatch.setattr(hw, "display_pil", lambda img: written.append(img.getpixel((20, 12))), raising=False)

        p = tmp_path / "anim.webp"
        _make_animated_webp(p, n=3)
        manager = self._manager(tmp_path)
        manager._hw_display_image = lambda img: None  # first-frame static render
        manager.display_from_file(p)
        self._wait_for_player(manager)
        time.sleep(0.25)
        manager.stop_animation()
        distinct = set(written)
        assert len(written) >= 3
        assert len(distinct) >= 2, "playback must cycle through distinct frames"

    def test_display_call_returns_fast_even_when_prep_is_slow(self, tmp_path, monkeypatch):
        """Regression: multi-frame preparation must happen in the background.
        A synchronous prep blocked the display command handler long enough
        to trip the server's 10s request timeout on real hardware."""
        import mimir_display.hardware as hw
        monkeypatch.setattr(hw, "supports_pil_playback", lambda: True, raising=False)
        monkeypatch.setattr(hw, "supports_frame_bytes", lambda: True, raising=False)

        def slow_prepare(frame):
            time.sleep(0.2)  # simulate Pi-speed conversion
            return b"frame"
        written = []
        monkeypatch.setattr(hw, "prepare_frame", slow_prepare, raising=False)
        monkeypatch.setattr(hw, "display_frame_bytes", lambda d: written.append(d), raising=False)

        p = tmp_path / "anim.webp"
        _make_animated_webp(p, n=4)
        manager = self._manager(tmp_path)
        manager._hw_display_image = lambda img: None

        started = time.monotonic()
        manager.display_from_file(p)
        elapsed = time.monotonic() - started
        assert elapsed < 0.15, f"display_from_file blocked for {elapsed:.2f}s — prep must be async"

        self._wait_for_player(manager)  # playback still arrives afterwards
        manager.stop_animation()

    def test_superseded_prep_never_installs_player(self, tmp_path, monkeypatch):
        import mimir_display.hardware as hw
        monkeypatch.setattr(hw, "supports_pil_playback", lambda: True, raising=False)
        monkeypatch.setattr(hw, "supports_frame_bytes", lambda: True, raising=False)
        monkeypatch.setattr(hw, "prepare_frame", lambda f: (time.sleep(0.1), b"x")[1], raising=False)
        monkeypatch.setattr(hw, "display_frame_bytes", lambda d: None, raising=False)

        anim = tmp_path / "anim.webp"
        _make_animated_webp(anim, n=4)
        static = tmp_path / "still.png"
        Image.new("RGB", (40, 24), (9, 9, 9)).save(static)

        manager = self._manager(tmp_path)
        manager._hw_display_image = lambda img: None
        manager.display_from_file(anim)     # prep starts in background (~0.4s)
        manager.display_from_file(static)   # supersedes it immediately
        time.sleep(0.8)                     # give the stale prep time to finish
        assert manager._anim_player is None, "superseded prep must not install a player"

    def test_animated_file_falls_back_to_first_frame_without_pil(self, tmp_path, monkeypatch):
        import mimir_display.hardware as hw
        monkeypatch.setattr(hw, "supports_pil_playback", lambda: False, raising=False)

        p = tmp_path / "anim.webp"
        _make_animated_webp(p, n=3)
        manager = self._manager(tmp_path)
        rendered = {}
        manager._hw_display_image = lambda img: rendered.setdefault("img", img.copy())
        manager.display_from_file(p)
        assert manager._anim_player is None
        r, g, b = rendered["img"].getpixel((20, 12))
        assert r > 200 and g < 60, "static fallback must show the first (red) frame"

    def test_new_static_content_stops_running_animation(self, tmp_path, monkeypatch):
        import mimir_display.hardware as hw
        monkeypatch.setattr(hw, "supports_pil_playback", lambda: True, raising=False)
        monkeypatch.setattr(hw, "supports_frame_bytes", lambda: False, raising=False)
        monkeypatch.setattr(hw, "display_pil", lambda img: None, raising=False)

        anim = tmp_path / "anim.webp"
        _make_animated_webp(anim, n=3)
        static = tmp_path / "still.png"
        Image.new("RGB", (40, 24), (9, 9, 9)).save(static)

        manager = self._manager(tmp_path)
        manager._hw_display_image = lambda img: None
        manager.display_from_file(anim)
        player = self._wait_for_player(manager)

        manager.display_from_file(static)
        assert manager._anim_player is None
        assert not player.running
