from __future__ import annotations

import asyncio
import json
import logging
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse, urlunparse


# Try to import aiohttp - if not available, we'll handle it gracefully in refresh
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
    from aiomqtt import Client

class MqttCommandHandler:
    """Handles incoming MQTT commands from the service with assignment processing."""
    
    def __init__(self, topics: MqttTopicManager, assignment_processor: AssignmentProcessor | None = None, capabilities: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None, display_id: str | None = None, platform_url: str | None = None):
        self.topics = topics
        self.assignment_processor = assignment_processor
        self.capabilities = capabilities or {}
        self.metadata = metadata or {}
        self.display_id = display_id
        self.platform_url = platform_url
        self.logger = logging.getLogger(__name__)
        self.command_handlers: dict[str, Callable] = {}
        self._event_publisher: MqttEventPublisher | None = None
        self._mqtt_client: Client | None = None
        # set_scene(scene_id, subchannel_id, assignment_id=..., source=...)
        self._set_scene_cb: Callable[[str, str | None], Any] | None = None
        self._clear_scene_cb: Callable[..., Any] | None = None
        self._presence_manager: MqttPresenceManager | None = None
        self._registration: Any | None = None  # MqttRegistrationManager, set externally

        # Register built-in handlers
        self.register_handler("assign", self._handle_assignment)
        self.register_handler("refresh", self._handle_refresh)
        self.register_handler("register", self._handle_register_request)
        self.register_handler("ready", self._handle_ready)
        self.register_handler("registration_complete", self._handle_registration_complete)
        self.register_handler("finalize_registration", self._handle_finalize_registration)
        self.register_handler("update_client", self._handle_update_client)
        self.register_handler("display_image", self._handle_display_image)
        self.register_handler("set_scene", self._handle_set_scene)
        self.register_handler("clear_scene", self._handle_clear_scene)
        
        # Initialize current scene and subchannel IDs
        self.current_scene_id: str | None = None
        self.current_subchannel_id: str | None = None
    
    def set_event_publisher(self, event_publisher: MqttEventPublisher):
        """Set the event publisher for sending responses."""
        self._event_publisher = event_publisher
    
    def set_presence_manager(self, presence_manager: MqttPresenceManager):
        """Set the presence manager for updating heartbeat fields."""
        self._presence_manager = presence_manager
    
    def set_mqtt_client(self, client: Client):
        """Set the MQTT client for sending registration responses."""
        self._mqtt_client = client
        # Also set the client for the event publisher if available
        if self._event_publisher:
            self._event_publisher.set_client(client)
    
    async def handle_command_message(self, message):
        """Handle a raw MQTT command message."""
        if message.topic.value == self.topics.commands:
            await self._handle_command(message.payload.decode())
    
    def register_handler(self, command_type: str, handler: Callable):
        """Register a handler for a specific command type."""
        self.command_handlers[command_type] = handler
        self.logger.info("Registered handler for command type: %s", command_type)
    
    async def start_listening(self, client: Client):
        """Start listening for commands."""
        self.set_mqtt_client(client)  # Store the client for event publishing
        await client.subscribe(self.topics.commands, qos=1)
        self.logger.info("Subscribed to commands at %s", self.topics.commands)
        
        async for message in client.messages:
            if message.topic.value == self.topics.commands:
                await self._handle_command(message.payload.decode())
    
    async def _handle_command(self, payload: str):
        """Handle incoming command."""
        try:
            command = json.loads(payload)
            command_type = command.get("type") or command.get("action")
            assignment_id = command.get("assignment_id", "unknown")
            
            self.logger.info(
                "Received command: %s (assignment: %s)", command_type, assignment_id
            )
            
            if command_type in self.command_handlers:
                handler = self.command_handlers[command_type]
                if asyncio.iscoroutinefunction(handler):
                    await handler(command)
                else:
                    handler(command)
            else:
                self.logger.warning("No handler for command type: %s", command_type)
                # Send error event for unknown command type
                if self._event_publisher:
                    await self._event_publisher.publish_error(
                        assignment_id=assignment_id,
                        error_type="unknown_command",
                        message=f"No handler for command type: {command_type}"
                    )
                
        except json.JSONDecodeError as e:
            self.logger.error("Invalid command JSON: %s", e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    error_type="invalid_json",
                    message=f"Command payload is not valid JSON: {e}"
                )
        except Exception as e:
            self.logger.error("Error handling command: %s", e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    error_type="command_processing",
                    message=f"Command processing failed: {e}"
                )
    
    async def _handle_assignment(self, command: dict[str, Any]):
        """Handle assignment command with content download and display."""
        assignment_id = command.get("assignment_id")
        sequence = command.get("sequence")
        # scene_id = command.get("scene_id")

        # if scene_id and hasattr(self, "_on_assignment_scene_hint"):
        #     try:
        #         self._on_assignment_scene_hint(scene_id) 
        #     except Exception:
        #         pass
        
        try:
            # Send ACK immediately
            if self._event_publisher:
                await self._event_publisher.publish_ack(
                    assignment_id=assignment_id,
                    sequence=sequence,
                    success=True
                )

            # Process assignment if processor is available
            if self.assignment_processor:
                start_time = datetime.now()
                result = await self.assignment_processor.process_assignment(command)
                duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
                
                if result.get("success"):
                    # Send rendered event
                    if self._event_publisher:
                        await self._event_publisher.publish_rendered(
                            assignment_id=assignment_id,
                            duration_ms=duration_ms
                        )
                    self.logger.info(
                        "Assignment %s completed successfully in %dms", assignment_id, duration_ms
                    )
                else:
                    # Send error event
                    if self._event_publisher:
                        await self._event_publisher.publish_error(
                            assignment_id=assignment_id,
                            error_type=result.get("error_type", "processing_failed"),
                            message=result.get("error", "Assignment processing failed")
                        )
            else:
                self.logger.warning(
                    "No assignment processor configured for %s", assignment_id
                )
                if self._event_publisher:
                    await self._event_publisher.publish_error(
                        assignment_id=assignment_id,
                        error_type="no_processor",
                        message="Assignment processor not configured"
                    )
                
        except Exception as e:
            self.logger.error(
                "Assignment %s processing failed: %s", assignment_id, e
            )
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="assignment_error",
                    message=str(e)
                )
    
    async def _handle_set_scene(self, command: dict[str, Any]):
        """
        Handle set_scene command, storing scene_id and optional subchannel_id,
        and publish assignment acknowledgment.
        """
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
                    message="set_scene requires 'scene_id'"
                )
            return

        # Store scene_id and subchannel_id locally
        self.current_scene_id = scene_id
        self.current_subchannel_id = subchannel_id

        try:
            # Call the scene callback to update the client state and presence (forward assignment_id)
            if self._set_scene_cb:
                await self._set_scene_cb(scene_id, subchannel_id, assignment_id=assignment_id, source="set_scene")
            # Explicit MQTT ack event for scene assignment
            if self._event_publisher:
                msg = f"scene_id set to {scene_id}"
                if subchannel_id:
                    msg += f", subchannel_id set to {subchannel_id}"
                await self._event_publisher.publish_ack(
                    assignment_id=assignment_id,
                    success=True,
                    message=msg,
                    scene_id=scene_id,
                    subchannel_id=subchannel_id
                )
        except Exception as e:
            self.logger.error("set_scene failed: %s", e, exc_info=True)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="set_scene_failed",
                    message=str(e)
                )

    async def _handle_clear_scene(self, command: dict[str, Any]):
        assignment_id = command.get("assignment_id", "clear_scene")
        try:
            if self._clear_scene_cb:
                await self._clear_scene_cb(assignment_id=assignment_id, reason="clear_scene")
            if self._event_publisher:
                await self._event_publisher.publish_ack(
                    assignment_id=assignment_id,
                    success=True,
                    message="scene_id cleared"
                )
        except Exception as e:
            self.logger.error("clear_scene failed: %s", e, exc_info=True)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="clear_scene_failed",
                    message=str(e)
                )

    async def _handle_refresh(self, command: dict[str, Any]):
        """Handle refresh command - the API service should send display_image command with new content."""
        assignment_id = command.get("assignment_id", "refresh")
        self.logger.info("Received refresh command %s", assignment_id)

        # Send immediate ACK
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=assignment_id,
                success=True,
                message="Refresh request acknowledged"
            )

        # Check if we have a scene assigned
        if not self.current_scene_id:
            self.logger.info("No scene currently assigned to display")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="no_scene_assigned",
                    message="No scene currently assigned to display"
                )
            return

        self.logger.info(
            "Refresh acknowledged for scene %s. Waiting for API to send display_image command with fresh content.",
            self.current_scene_id,
        )

    def set_registration_manager(self, registration: Any) -> None:
        """Provide access to MqttRegistrationManager so finalize_registration can persist state."""
        self._registration = registration

    def set_scene_callbacks(self, set_cb: Callable[[str, str | None], Any], clear_cb: Callable[[], Any]):
        self._set_scene_cb = set_cb
        self._clear_scene_cb = clear_cb
    
    async def _handle_register_request(self, command: dict[str, Any]):
        """Handle registration request from API service - send our details back."""
        self.logger.info("Received registration request from API: %s", command)
        
        reply_to = command.get("reply_to")
        if not reply_to:
            self.logger.error("Registration request missing reply_to topic")
            return
            
        if not self._mqtt_client:
            self.logger.error("No MQTT client available for sending registration response")
            return
            
        try:
            # Prepare registration details using our actual capabilities and metadata
            registration_data = {
                "device_id": self.topics.device_id,
                "capabilities": self.capabilities,
                "metadata": self.metadata,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            self.logger.info("Sending registration details to %s", reply_to)
            self.logger.debug("Registration data: %s", registration_data)
            
            # Send registration response via MQTT
            await self._mqtt_client.publish(
                reply_to,
                json.dumps(registration_data),
                qos=1
            )
            
            self.logger.info("Registration details sent successfully")
                
        except Exception as e:  # broad: network / serialization errors
            self.logger.error("Error handling registration request: %s", e)
    
    async def _handle_ready(self, command: dict[str, Any]):
        """Handle ready acknowledgment from API - device confirms it is operational.

        This command does not currently trigger any display action; it simply acknowledges
        registration/handshake steps. Additional fields in the payload are ignored but logged.
        """
        self.logger.info("Received ready acknowledgment: %s", command)
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "ready"),
                success=True,
                message=command.get("message", "Ready acknowledgment received")
            )
    
    async def _handle_registration_complete(self, command: dict[str, Any]):
        """Handle registration complete confirmation from API.

        Logs final registration success and sends an ACK back to the service.
        """
        self.logger.info("Registration complete: %s", command)
        display_id = command.get("display_id")
        message = command.get("message", "Registration complete")
        self.logger.info(
            "Registration successful - ID: %s, Message: %s", display_id, message
        )
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "registration_complete"),
                success=True,
                message=message,
            )

    async def _handle_finalize_registration(self, command: dict[str, Any]):
        """Handle finalize_registration sent by the server after a pairing claim.

        The pairing flow is:
          Display publishes pair request → server stores code → user enters code in UI
          → server creates DB record → server sends finalize_registration to display/cmd

        This is the 'you have been claimed and registered' signal.  We persist
        the assigned display_id so is_registered() returns True on next start.
        """
        display_id: str | None = command.get("display_id")
        registration_key: str | None = command.get("registration_key")
        self.logger.info(
            "Received finalize_registration display_id=%s", display_id
        )

        # Persist registration state
        if hasattr(self, "_registration") and self._registration is not None:
            try:
                self._registration.update_registration(
                    device_id=self.topics.device_id,
                    assigned_id=display_id or self.topics.device_id,
                    service_config={"registration_key": registration_key} if registration_key else {},
                )
                self.logger.info("Registration state persisted for display_id=%s", display_id)
            except Exception as e:
                self.logger.warning("Could not persist registration state: %s", e)

        # Persist server-assigned config (platform URL, MQTT details, name, location)
        try:
            from mimir_display.storage.device_config import DeviceConfig
            dc = DeviceConfig()
            dc.apply_finalize_payload(command)
        except Exception as e:
            self.logger.warning("Could not persist device config: %s", e)

        # Notify via ack
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "finalize_registration"),
                success=True,
                message=f"Pairing complete — display_id={display_id}",
            )

    async def _handle_update_client(self, command: dict[str, Any]):
        """Handle update_client command sent from the server.

        Triggers pull_and_update.sh in a detached subprocess so the service
        can restart itself without losing the MQTT ack.

        Command payload (all optional):
            branch   str   git branch to pull (default: "main")
            dry_run  bool  pass DRY_RUN=1 to the script (default: false)
        """
        assignment_id = command.get("assignment_id", "update_client")
        branch = str(command.get("branch", "main"))
        dry_run = bool(command.get("dry_run", False))

        self.logger.info(
            "Received update_client command branch=%s dry_run=%s", branch, dry_run
        )

        # ACK immediately so the server sees confirmation before the service restarts
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=assignment_id,
                success=True,
                message=f"Update triggered (branch={branch})",
            )

        from mimir_display.utils.update import trigger_update
        pid = trigger_update(git_branch=branch, dry_run=dry_run, log=self.logger)
        if pid:
            self.logger.info("Update script launched (pid=%d)", pid)
        else:
            self.logger.warning(
                "update_client: could not locate update script — "
                "set MIMIR_REPO_DIR to the git checkout root"
            )

    # ---------------------------------------------------------------------
    # Display Image Handling
    # ---------------------------------------------------------------------
    def _pretty_display_target(self, image_url: str, max_len: int = 64) -> str:
        """Return a short human-friendly target string for ACK messages.

        Attempts to use the basename of the path; falls back to the whole URL.
        Truncates overly long names for readability.
        """
        try:
            parsed = urlparse(image_url)
            basename = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
            target = basename or image_url
        except Exception:
            target = image_url
        if len(target) > max_len:
            return target[: max_len - 1] + "…"
        return target

    async def _handle_display_image(self, command: dict[str, Any]):
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
            self.logger.debug("Pre-resolved .local hostname %s -> %s", host_header, urlparse(url).hostname)

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
                            self.logger.info("Resolved .local hostname %s (retrying)", original_host)
                            url = retried_url
                            continue  # retry immediately with rebuilt URL
            raise last_exc  # All retries exhausted

    async def _render_local_file(self, path: Path) -> None:
        """Call the same display callback path the assignment processor uses."""
        # Reuse the same callback the manager provided to the MQTT client
        if self.assignment_processor and callable(self.assignment_processor.display_callback):
            await self.assignment_processor.display_callback(path, display_config={})
        else:
            # As an ultimate fallback, import the manager-level display directly if needed
            raise RuntimeError("Display callback unavailable; cannot render image")
