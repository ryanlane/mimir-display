from __future__ import annotations

import logging
import os
from typing import Any, Callable

from .content import DisplayManager
from .content.splash import build_splash, get_local_ip, overlay_status


class SplashRenderer:
    """Builds and updates the startup/pairing splash screen.

    Owns all splash image state so MqttDisplayClientManager does not need to
    track _splash_path, _current_splash_status, or the render signature.
    """

    def __init__(
        self,
        *,
        config,
        data_dir: str,
        pair_code: str,
        logger: logging.Logger,
        get_capabilities: Callable[[], dict[str, Any]],
    ) -> None:
        self._config = config
        self._data_dir = data_dir
        self._cache_dir = os.path.join(data_dir, "cache")
        self._pair_code = pair_code
        self.logger = logger
        self._get_capabilities = get_capabilities

        self._splash_path: str | None = None
        self._current_splash_status: str = ""
        self._current_splash_is_error: bool = False
        self._last_splash_signature: tuple | None = None

    @property
    def current_status(self) -> str:
        return self._current_splash_status

    def update_status(self, text: str, is_error: bool = False) -> None:
        """Update status text, only redrawing the splash for error-state changes."""
        if not self._splash_path or not os.path.exists(self._splash_path):
            return
        try:
            if text == self._current_splash_status and is_error == self._current_splash_is_error:
                return

            self._current_splash_status = text
            self._current_splash_is_error = is_error

            # Avoid extra e-ink refreshes for normal progress updates.
            # The status is still tracked so the next full splash render can include it.
            if not is_error:
                self.logger.debug("Splash status updated without redraw: %s", text)
                return

            updated = overlay_status(self._splash_path, text, is_error=is_error)
            if updated is None:
                return
            updated.save(self._splash_path, format="PNG")
            capabilities = self._get_capabilities()
            dm = DisplayManager(capabilities, self._cache_dir, self.logger)
            dm.display_from_file(self._splash_path)
        except Exception as e:  # noqa: BLE001
            self.logger.debug("Failed to update splash status: %s", e)

    def render_startup(self, status_text: str = "") -> None:
        """Build and display the startup/pairing splash screen."""
        try:
            logo_path = (
                os.environ.get("STARTUP_LOGO_PATH")
                or os.path.join(os.path.dirname(__file__), "images", "startup.png")
            )
            capabilities = self._get_capabilities()
            res = capabilities.get("resolution") or [800, 480]
            splash_w, splash_h = int(res[0]), int(res[1])
            splash_img = build_splash(
                width=splash_w,
                height=splash_h,
                pair_code=self._pair_code,
                platform_url=self._config.platform_url or None,
                ip_address=get_local_ip(),
                logo_path=logo_path if os.path.exists(logo_path) else None,
                status_text=status_text,
            )

            splash_signature = (
                self._pair_code,
                self._config.platform_url or None,
                get_local_ip(),
                status_text,
                self._current_splash_is_error,
                splash_w,
                splash_h,
            )
            if splash_signature == self._last_splash_signature:
                self.logger.debug("Skipping identical startup splash render")
                return

            splash_path = os.path.join(self._cache_dir, "startup_splash.png")
            os.makedirs(os.path.dirname(splash_path), exist_ok=True)
            splash_img.save(splash_path, format="PNG")
            self._splash_path = splash_path
            self._current_splash_status = status_text
            self._last_splash_signature = splash_signature

            tmp_dm = DisplayManager(capabilities, self._cache_dir, self.logger)
            tmp_dm.display_from_file(splash_path)
            self.logger.info(
                "Startup splash displayed (pair_code=%s, size=%dx%d, platform_url=%s)",
                self._pair_code,
                splash_w,
                splash_h,
                self._config.platform_url or "(unset)",
            )
        except Exception as e:  # noqa: BLE001 - non-fatal
            self.logger.debug("Startup splash failed: %s", e, exc_info=True)

    def handle_pair_status(self, status: str, payload: dict[str, Any]) -> None:
        """Reflect pair-code readiness on the splash screen."""
        if status in ("ok", "pending"):
            self.update_status("Connected — enter code in Mimir to pair")
            return

        if status == "error":
            message = str(payload.get("message") or "Pairing setup failed")
            self.update_status(message, is_error=True)
