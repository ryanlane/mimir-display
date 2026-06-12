from __future__ import annotations

import asyncio
import logging
import random
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse, urlunparse

try:
    from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    # Provide stubs for type checking
    ClientError = Exception
    ClientResponseError = Exception
    ClientSession = None
    ClientTimeout = None

from mimir_display.content.downloader import AssignmentProcessor
from mimir_display.utils.helpers import resolve_dot_local_url

from .topics import MqttTopicManager

if TYPE_CHECKING:
    from .events import MqttEventPublisher
    from .presence import MqttPresenceManager


class DisplayCommandHandler:
    """Handles MQTT commands related to content display and scene management.

    Commands: assign, display_image, set_scene, clear_scene, refresh
    """

    def __init__(
        self,
        topics: MqttTopicManager,
        assignment_processor: AssignmentProcessor | None = None,
    ):
        self.topics = topics
        self.assignment_processor = assignment_processor
        self.logger = logging.getLogger(__name__)
        self._event_publisher: MqttEventPublisher | None = None
        self._presence_manager: MqttPresenceManager | None = None
        self._set_scene_cb: Callable[[str, str | None], Any] | None = None
        self._clear_scene_cb: Callable[..., Any] | None = None
        self.current_scene_id: str | None = None
        self.current_subchannel_id: str | None = None

    def set_event_publisher(self, event_publisher: MqttEventPublisher) -> None:
        self._event_publisher = event_publisher

    def set_presence_manager(self, presence_manager: MqttPresenceManager) -> None:
        self._presence_manager = presence_manager

    def set_scene_callbacks(
        self,
        set_cb: Callable[[str, str | None], Any],
        clear_cb: Callable[[], Any],
    ) -> None:
        self._set_scene_cb = set_cb
        self._clear_scene_cb = clear_cb

    async def handle_assign(self, command: dict[str, Any]) -> None:
        """Handle assignment command with content download and display."""
        assignment_id = command.get("assignment_id")
        sequence = command.get("sequence")

        try:
            if self._event_publisher:
                await self._event_publisher.publish_ack(
                    assignment_id=assignment_id,
                    sequence=sequence,
                    success=True,
                )

            if self.assignment_processor:
                start_time = datetime.now()
                result = await self.assignment_processor.process_assignment(command)
                duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                if result.get("success"):
                    if self._event_publisher:
                        await self._event_publisher.publish_rendered(
                            assignment_id=assignment_id,
                            duration_ms=duration_ms,
                        )
                    self.logger.info(
                        "Assignment %s completed successfully in %dms", assignment_id, duration_ms
                    )
                else:
                    if self._event_publisher:
                        await self._event_publisher.publish_error(
                            assignment_id=assignment_id,
                            error_type=result.get("error_type", "processing_failed"),
                            message=result.get("error", "Assignment processing failed"),
                        )
            else:
                self.logger.warning(
                    "No assignment processor configured for %s", assignment_id
                )
                if self._event_publisher:
                    await self._event_publisher.publish_error(
                        assignment_id=assignment_id,
                        error_type="no_processor",
                        message="Assignment processor not configured",
                    )

        except Exception as e:
            self.logger.error("Assignment %s processing failed: %s", assignment_id, e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="assignment_error",
                    message=str(e),
                )

    async def handle_set_scene(self, command: dict[str, Any]) -> None:
        """Handle set_scene command, storing scene_id and optional subchannel_id."""
        assignment_id = command.get("assignment_id", "set_scene")
        scene_id = command.get("scene_id")
        subchannel_id = command.get("subchannel_id")  # Optional

        self.logger.info(
            "Handling set_scene: scene_id=%s, subchannel_id=%s, assignment_id=%s",
            scene_id,
            subchannel_id,
            assignment_id,
        )

        if not scene_id:
            self.logger.error("set_scene missing scene_id")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="invalid_command",
                    message="set_scene requires 'scene_id'",
                )
            return

        self.current_scene_id = scene_id
        self.current_subchannel_id = subchannel_id

        try:
            if self._set_scene_cb:
                await self._set_scene_cb(
                    scene_id, subchannel_id, assignment_id=assignment_id, source="set_scene"
                )
            if self._event_publisher:
                msg = f"scene_id set to {scene_id}"
                if subchannel_id:
                    msg += f", subchannel_id set to {subchannel_id}"
                await self._event_publisher.publish_ack(
                    assignment_id=assignment_id,
                    success=True,
                    message=msg,
                    scene_id=scene_id,
                    subchannel_id=subchannel_id,
                )
        except Exception as e:
            self.logger.error("set_scene failed: %s", e, exc_info=True)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="set_scene_failed",
                    message=str(e),
                )

    async def handle_clear_scene(self, command: dict[str, Any]) -> None:
        """Handle clear_scene command."""
        assignment_id = command.get("assignment_id", "clear_scene")
        try:
            if self._clear_scene_cb:
                await self._clear_scene_cb(assignment_id=assignment_id, reason="clear_scene")
            if self._event_publisher:
                await self._event_publisher.publish_ack(
                    assignment_id=assignment_id,
                    success=True,
                    message="scene_id cleared",
                )
        except Exception as e:
            self.logger.error("clear_scene failed: %s", e, exc_info=True)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="clear_scene_failed",
                    message=str(e),
                )

    async def handle_refresh(self, command: dict[str, Any]) -> None:
        """Handle refresh command — the API service should send display_image with new content."""
        assignment_id = command.get("assignment_id", "refresh")
        self.logger.info("Received refresh command %s", assignment_id)

        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=assignment_id,
                success=True,
                message="Refresh request acknowledged",
            )

        if not self.current_scene_id:
            self.logger.info("No scene currently assigned to display")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="no_scene_assigned",
                    message="No scene currently assigned to display",
                )
            return

        self.logger.info(
            "Refresh acknowledged for scene %s. Waiting for API to send display_image command with fresh content.",
            self.current_scene_id,
        )

    def _pretty_display_target(self, image_url: str, max_len: int = 64) -> str:
        """Return a short human-friendly target string for ACK messages."""
        try:
            parsed = urlparse(image_url)
            basename = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
            target = basename or image_url
        except Exception:
            target = image_url
        if len(target) > max_len:
            return target[: max_len - 1] + "…"
        return target

    async def handle_display_image(self, command: dict[str, Any]) -> None:
        """Handle a display_image command.

        Strategy:
        1. Attempt normal assignment processing by synthesising a minimal assignment
           structure that the existing AssignmentProcessor understands.
        2. If that fails (KeyError / other), fall back to a direct download + render.
        """
        self.logger.info("Received display image command: %s", command)
        assignment_id = command.get("assignment_id", "display_image")
        image_url = command.get("image_url") or command.get("url")

        if not image_url:
            self.logger.error("display_image missing image_url/url")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="display_error",
                    message="Missing image_url/url in display_image command",
                )
            return

        # Immediate optimistic ACK (lets UI show progress quickly)
        if self._event_publisher:
            target_msg = f"Displaying: {self._pretty_display_target(image_url)}"
            await self._event_publisher.publish_ack(
                assignment_id=assignment_id,
                success=True,
                message=target_msg,
            )

        # Preferred path: reuse assignment processor so pipelines (caching, scaling, etc.) apply.
        if self.assignment_processor:
            start_time = datetime.now()
            try:
                pseudo_assignment = {
                    "assignment_id": assignment_id,
                    "content": {"delivery": {"type": "url", "url": image_url}},
                }
                result = await self.assignment_processor.process_assignment(pseudo_assignment)
                duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)

                if not result.get("success"):
                    # Retry with top-level delivery variant some older code accepted.
                    alt_assignment = {
                        "assignment_id": assignment_id,
                        "delivery": {"type": "url", "url": image_url},
                    }
                    result = await self.assignment_processor.process_assignment(alt_assignment)

                if result.get("success"):
                    if self._event_publisher:
                        await self._event_publisher.publish_rendered(
                            assignment_id=assignment_id, duration_ms=duration_ms
                        )
                    self.logger.info(
                        "Displayed image via assignment pipeline in %dms", duration_ms
                    )
                    return
                else:
                    self.logger.warning(
                        "Assignment processor failed (result=%s); falling back to direct download",
                        result,
                    )
            except KeyError as e:
                self.logger.warning(
                    "Assignment processor KeyError %s; falling back to direct download", e
                )
            except Exception as e:
                self.logger.exception(
                    "Assignment processor exception (%s); falling back to direct download", e
                )

        # Fallback path: direct download then render with display callback.
        try:
            start_time = datetime.now()
            tmp_path = await self._download_to_temp(image_url)
            await self._render_local_file(tmp_path)
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            if self._event_publisher:
                await self._event_publisher.publish_rendered(
                    assignment_id=assignment_id, duration_ms=duration_ms
                )
            self.logger.info(
                "Displayed image via direct fallback in %dms (path=%s)", duration_ms, tmp_path
            )
        except Exception as e:
            self.logger.exception("display_image fallback failed: %s", e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="display_exception",
                    message=str(e),
                )

    async def _download_to_temp(self, url: str) -> Path:
        """Download URL to a temp file asynchronously with retries and .local fallback.

        If aiohttp isn't available we raise immediately; the higher-level handler will
        surface the error as a display_exception event.
        """
        if not AIOHTTP_AVAILABLE:
            raise RuntimeError("aiohttp not installed; cannot download image")

        attempts = 3
        backoff = 0.75  # seconds
        original_host = urlparse(url).hostname or ""

        # Pre-resolve .local before first attempt so we don't rely on aiohttp resolver
        url, host_header = resolve_dot_local_url(url)
        if host_header:
            self.logger.debug(
                "Pre-resolved .local hostname %s -> %s", host_header, urlparse(url).hostname
            )

        async with ClientSession() as session:
            last_exc = None
            for i in range(1, attempts + 1):
                try:
                    req_headers = {"Host": host_header} if host_header else None
                    async with session.get(url, timeout=ClientTimeout(total=20), headers=req_headers) as resp:
                        if resp.status >= 500 or resp.status == 429:
                            raise ClientResponseError(
                                request_info=resp.request_info,
                                history=resp.history,
                                status=resp.status,
                                message=f"server returned {resp.status}",
                                headers=resp.headers,
                            )
                        resp.raise_for_status()
                        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                        ext = {
                            "image/png": ".png",
                            "image/jpeg": ".jpg",
                            "image/webp": ".webp",
                            "image/gif": ".gif",
                            "image/bmp": ".bmp",
                        }.get(ctype, ".img")
                        data = await resp.read()
                        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                            f.write(data)
                            return Path(f.name)
                except (ClientResponseError, ClientError, asyncio.TimeoutError) as e:
                    last_exc = e
                    if i == attempts:
                        break
                    await asyncio.sleep(backoff * (1 + random.random() * 0.3))
                    backoff *= 1.6
                except Exception as e:
                    last_exc = e
                    if i < attempts and original_host.endswith(".local"):
                        retried_url, _ = resolve_dot_local_url(
                            urlunparse(urlparse(url)._replace(netloc=original_host))
                        )
                        if retried_url != url:
                            self.logger.info(
                                "Resolved .local hostname %s (retrying)", original_host
                            )
                            url = retried_url
                            continue  # retry immediately with rebuilt URL
            raise last_exc  # All retries exhausted

    async def _render_local_file(self, path: Path) -> None:
        """Call the same display callback path the assignment processor uses."""
        if self.assignment_processor and callable(self.assignment_processor.display_callback):
            await self.assignment_processor.display_callback(path, display_config={})
        else:
            raise RuntimeError("Display callback unavailable; cannot render image")
