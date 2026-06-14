"""Unit tests for OtaUpdateManager: desired-version evaluation, skip rules,
failure backoff, and presence fields."""
import json
import os
import time

import pytest

import mimir_display.ota as ota_module
from mimir_display.ota import FAILED_RETRY_SECONDS, OtaUpdateManager


class FakeConfig:
    def __init__(self, **kv):
        self._kv = kv
        self.platform_url = kv.get("platform_url")

    def get(self, key, default=None):
        return self._kv.get(key, default)


def desired(version="1.0.7", phase="all", **overrides):
    payload = {
        "version": version,
        "phase": phase,
        "artifact": f"mimir_display-{version}.tar.gz",
        "sha256": "deadbeef",
        "download_path": f"/api/client-releases/v{version}/download",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("OTA_DIR", str(tmp_path))
    monkeypatch.setattr(ota_module, "CLIENT_VERSION", "1.0.5")
    return OtaUpdateManager(FakeConfig(platform_url="http://oak.local:5000"))


def read_request(manager):
    return json.loads((manager.ota_dir / "request.json").read_text())


class TestHandleDesiredVersion:
    def test_writes_request_for_new_version(self, manager):
        assert manager.handle_desired_version(desired("1.0.7")) is True

        req = read_request(manager)
        assert req["version"] == "1.0.7"
        assert req["current_version"] == "1.0.5"
        assert req["sha256"] == "deadbeef"
        assert req["download_url"] == "http://oak.local:5000/api/client-releases/v1.0.7/download"

    def test_skips_when_already_on_target(self, manager):
        assert manager.handle_desired_version(desired("1.0.5")) is False
        assert not (manager.ota_dir / "request.json").exists()

    def test_version_prefix_normalized(self, manager):
        # v-prefixed desired version matching the running version is a no-op
        assert manager.handle_desired_version(desired("v1.0.5")) is False

    def test_empty_version_ignored(self, manager):
        assert manager.handle_desired_version(desired("")) is False

    def test_pending_request_not_rewritten(self, manager):
        assert manager.handle_desired_version(desired("1.0.7")) is True
        first_mtime = (manager.ota_dir / "request.json").stat().st_mtime_ns

        assert manager.handle_desired_version(desired("1.0.7")) is False
        assert (manager.ota_dir / "request.json").stat().st_mtime_ns == first_mtime

    def test_new_version_replaces_pending_request(self, manager):
        manager.handle_desired_version(desired("1.0.6"))

        assert manager.handle_desired_version(desired("1.0.7")) is True
        assert read_request(manager)["version"] == "1.0.7"

    def test_missing_fields_rejected(self, manager):
        assert manager.handle_desired_version(desired("1.0.7", sha256=None)) is False
        assert manager.handle_desired_version(desired("1.0.7", download_path=None)) is False

    def test_missing_platform_url_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTA_DIR", str(tmp_path))
        monkeypatch.setattr(ota_module, "CLIENT_VERSION", "1.0.5")
        manager = OtaUpdateManager(FakeConfig(platform_url=None))

        assert manager.handle_desired_version(desired("1.0.7")) is False


class TestCanaryPhase:
    def test_canary_phase_skipped_by_regular_display(self, manager):
        assert manager.handle_desired_version(desired("1.0.7", phase="canary")) is False

    def test_canary_phase_honored_by_canary_display(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTA_DIR", str(tmp_path))
        monkeypatch.setattr(ota_module, "CLIENT_VERSION", "1.0.5")
        manager = OtaUpdateManager(
            FakeConfig(platform_url="http://oak.local:5000", display_tags="kitchen, canary")
        )

        assert manager.handle_desired_version(desired("1.0.7", phase="canary")) is True

    def test_all_phase_applies_to_everyone(self, manager):
        assert manager.handle_desired_version(desired("1.0.7", phase="all")) is True


class TestFailureBackoff:
    def write_status(self, manager, result="failed", target="1.0.7", age_seconds=0, error=None):
        status = {"result": result, "target_version": target}
        if error:
            status["error"] = error
        path = manager.ota_dir / "status.json"
        manager.ota_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status))
        if age_seconds:
            past = time.time() - age_seconds
            os.utime(path, (past, past))

    def test_recent_failure_backs_off(self, manager):
        self.write_status(manager, age_seconds=60)

        assert manager.handle_desired_version(desired("1.0.7")) is False

    def test_stale_failure_retries(self, manager):
        self.write_status(manager, age_seconds=FAILED_RETRY_SECONDS + 60)

        assert manager.handle_desired_version(desired("1.0.7")) is True

    def test_failure_for_other_version_does_not_block(self, manager):
        self.write_status(manager, target="1.0.6", age_seconds=60)

        assert manager.handle_desired_version(desired("1.0.7")) is True


class TestPresenceFields:
    def test_empty_without_status_or_canary(self, manager):
        assert manager.presence_fields() == {}

    def test_canary_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OTA_DIR", str(tmp_path))
        manager = OtaUpdateManager(FakeConfig(display_tags="canary"))

        assert manager.presence_fields() == {"canary": True}

    def test_status_surfaced(self, manager):
        manager.ota_dir.mkdir(parents=True, exist_ok=True)
        (manager.ota_dir / "status.json").write_text(json.dumps({
            "result": "failed",
            "target_version": "v1.0.7",
            "error": "x" * 500,
        }))

        fields = manager.presence_fields()
        assert fields["update_status"] == "failed"
        assert fields["update_target"] == "1.0.7"
        assert len(fields["update_error"]) == 200

    def test_invalid_status_json_is_empty(self, manager):
        manager.ota_dir.mkdir(parents=True, exist_ok=True)
        (manager.ota_dir / "status.json").write_text("{nope")

        assert manager.read_status() == {}
