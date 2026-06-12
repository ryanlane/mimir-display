import sys
from types import SimpleNamespace


def test_cli_accepts_hdmi(monkeypatch):
    # Simulate command line invocation
    monkeypatch.setenv("DISPLAY_BACKEND", "hdmi")
    argv_backup = sys.argv[:]
    sys.argv = ["mimir-display", "--backend", "hdmi"]

    # Patch loader to avoid opening windows
    fake_backend = SimpleNamespace(
        get_display_capabilities=lambda: {
            "backend": "hdmi",
            "resolution": [640, 360],
            "supported_formats": ["jpg"],
            "simulation_mode": True,
        }
    )

    def fake_load_backend(_explicit=None):  # noqa: D401
        return fake_backend

    from mimir_display import __main__ as entry

    monkeypatch.setattr(entry, "load_backend", fake_load_backend)

    async def _fake_runner() -> None:
        return None

    monkeypatch.setattr(entry, "runner", _fake_runner)

    try:
        assert entry.main() == 0
    finally:
        sys.argv = argv_backup
