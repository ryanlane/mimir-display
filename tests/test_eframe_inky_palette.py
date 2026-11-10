"""inky 2.6 + Pillow 13 'empty palette' fallback in the Inky backend."""

from __future__ import annotations

import pytest
from PIL import Image

from mimir_display.hardware import eframe_inky

PALETTE = [0, 0, 0, 255, 255, 255, 0, 255, 0, 0, 0, 255, 255, 0, 0, 255, 255, 0, 255, 140, 0, 255, 255, 255]


class FakeInky:
    """Mimics inky's AC073TC1A: set_image fails on non-P images like Pillow 13 does."""

    BLACK = 0

    def __init__(self, error: str = "empty palette") -> None:
        self.error = error
        self.shown: list[Image.Image] = []
        self.set_calls = 0

    def _palette_blend(self, saturation, dtype="uint8"):
        return list(PALETTE)

    def set_image(self, image, saturation=0.5):
        self.set_calls += 1
        if image.mode != "P":
            raise ValueError(self.error)
        self.shown.append(image)

    def set_border(self, colour):
        pass

    def show(self):
        pass


@pytest.fixture
def png(tmp_path):
    path = tmp_path / "frame.png"
    Image.new("RGB", (80, 48), (250, 10, 10)).save(path)
    return str(path)


@pytest.fixture
def backend(monkeypatch):
    def install(fake):
        monkeypatch.setattr(eframe_inky, "inky", fake)
        monkeypatch.setattr(eframe_inky, "simulation_mode", False)
        monkeypatch.setattr(eframe_inky, "_inky_initialized", True)
        return fake
    return install


def test_empty_palette_falls_back_to_prequantized_image(backend, png):
    fake = backend(FakeInky())
    eframe_inky.show_on_inky(png)
    assert fake.set_calls == 2
    (shown,) = fake.shown
    assert shown.mode == "P"
    assert shown.size == (80, 48)
    # Pure red maps to the panel's red entry (index 4), not to some other colour.
    assert shown.getpixel((40, 24)) == 4
    assert eframe_inky.simulation_mode is False


def test_other_value_errors_still_raise(backend, png):
    backend(FakeInky(error="Image must be (800x480) pixels!"))
    with pytest.raises(ValueError, match="800x480"):
        eframe_inky.show_on_inky(png)
