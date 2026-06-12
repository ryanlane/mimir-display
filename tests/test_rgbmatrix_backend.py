def test_rgbmatrix_backend_capabilities_simulation(monkeypatch):
    # Force rgbmatrix backend
    monkeypatch.setenv("DISPLAY_BACKEND", "rgbmatrix")
    # Ensure module not installed simulation path acceptable
    from mimir_display.hardware import get_display_capabilities  # type: ignore

    caps = get_display_capabilities()
    assert "rgbmatrix" in str(caps.get("backend", ""))
    assert "resolution" in caps
    # resolution should be a two-element list
    assert isinstance(caps["resolution"], list) and len(caps["resolution"]) == 2
    # simulation_mode may be True if library missing
    assert "simulation_mode" in caps
