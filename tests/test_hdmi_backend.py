def test_hdmi_backend_capabilities(monkeypatch):
    monkeypatch.setenv("DISPLAY_BACKEND", "hdmi")
    # Provide explicit resolution to avoid depending on actual desktop env.
    monkeypatch.setenv("HDMI_RESOLUTION", "640x360")
    from mimir_display.hardware import get_display_capabilities  # type: ignore

    caps = get_display_capabilities()
    assert caps["backend"] == "hdmi"
    assert caps["resolution"] == [640, 360]
    assert caps["native_resolution"] == [640, 360]
    assert caps["pixel_format"] == "RGB888"
    assert "supported_formats" in caps
