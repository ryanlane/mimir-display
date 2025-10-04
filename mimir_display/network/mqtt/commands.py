import asyncio
import json
import logging
import tempfile
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional, Callable, Any, TYPE_CHECKING
from urllib.parse import urlparse


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
from .topics import MqttTopicManager
from mimir_display.content.downloader import AssignmentProcessor

if TYPE_CHECKING:
    from .events import MqttEventPublisher
    from .presence import MqttPresenceManager
    from aiomqtt import Client

class MqttCommandHandler:
    """Handles incoming MQTT commands from the service with assignment processing."""
    
    def __init__(self, topics: MqttTopicManager, assignment_processor: AssignmentProcessor = None, capabilities: Dict[str, Any] = None, metadata: Dict[str, Any] = None, display_id: str = None, platform_url: str = None):
        self.topics = topics
        self.assignment_processor = assignment_processor
        self.capabilities = capabilities or {}
        self.metadata = metadata or {}
        self.display_id = display_id
        self.platform_url = platform_url
        self.logger = logging.getLogger(__name__)
        self.command_handlers: Dict[str, Callable] = {}
        self._event_publisher: Optional['MqttEventPublisher'] = None
        self._mqtt_client: Optional['Client'] = None
        self._set_scene_cb: Optional[Callable[[str], Any]] = None
        self._clear_scene_cb: Optional[Callable[[], Any]] = None
        self._presence_manager: Optional['MqttPresenceManager'] = None
        
        # Register built-in handlers
        self.register_handler("assign", self._handle_assignment)
        self.register_handler("refresh", self._handle_refresh)
        self.register_handler("register", self._handle_register_request)
        self.register_handler("ready", self._handle_ready)
        self.register_handler("registration_complete", self._handle_registration_complete)
        self.register_handler("display_image", self._handle_display_image)
        self.register_handler("set_scene", self._handle_set_scene)
        self.register_handler("clear_scene", self._handle_clear_scene)
        
        # Initialize current scene and subchannel IDs
        self.current_scene_id: Optional[str] = None
        self.current_subchannel_id: Optional[str] = None
    
    def set_event_publisher(self, event_publisher: 'MqttEventPublisher'):
        """Set the event publisher for sending responses."""
        self._event_publisher = event_publisher
    
    def set_presence_manager(self, presence_manager: 'MqttPresenceManager'):
        """Set the presence manager for updating heartbeat fields."""
        self._presence_manager = presence_manager
    
    def set_mqtt_client(self, client: 'Client'):
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
        self.logger.info(f"Registered handler for command type: {command_type}")
    
    async def start_listening(self, client: 'Client'):
        """Start listening for commands."""
        self.set_mqtt_client(client)  # Store the client for event publishing
        await client.subscribe(self.topics.commands, qos=1)
        self.logger.info(f"Subscribed to commands at {self.topics.commands}")
        
        async for message in client.messages:
            if message.topic.value == self.topics.commands:
                await self._handle_command(message.payload.decode())
    
    async def _handle_command(self, payload: str):
        """Handle incoming command."""
        try:
            command = json.loads(payload)
            command_type = command.get("type") or command.get("action")
            assignment_id = command.get("assignment_id", "unknown")
            
            self.logger.info(f"Received command: {command_type} (assignment: {assignment_id})")
            
            if command_type in self.command_handlers:
                handler = self.command_handlers[command_type]
                if asyncio.iscoroutinefunction(handler):
                    await handler(command)
                else:
                    handler(command)
            else:
                self.logger.warning(f"No handler for command type: {command_type}")
                # Send error event for unknown command type
                if self._event_publisher:
                    await self._event_publisher.publish_error(
                        assignment_id=assignment_id,
                        error_type="unknown_command",
                        message=f"No handler for command type: {command_type}"
                    )
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Invalid command JSON: {e}")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    error_type="invalid_json",
                    message=f"Command payload is not valid JSON: {e}"
                )
        except Exception as e:
            self.logger.error(f"Error handling command: {e}")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    error_type="command_processing",
                    message=f"Command processing failed: {e}"
                )
    
    async def _handle_assignment(self, command: Dict[str, Any]):
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
                    self.logger.info(f"Assignment {assignment_id} completed successfully in {duration_ms}ms")
                else:
                    # Send error event
                    if self._event_publisher:
                        await self._event_publisher.publish_error(
                            assignment_id=assignment_id,
                            error_type=result.get("error_type", "processing_failed"),
                            message=result.get("error", "Assignment processing failed")
                        )
            else:
                self.logger.warning(f"No assignment processor configured for {assignment_id}")
                if self._event_publisher:
                    await self._event_publisher.publish_error(
                        assignment_id=assignment_id,
                        error_type="no_processor",
                        message="Assignment processor not configured"
                    )
                
        except Exception as e:
            self.logger.error(f"Assignment {assignment_id} processing failed: {e}")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="assignment_error",
                    message=str(e)
                )
    
    async def _handle_set_scene(self, command: Dict[str, Any]):
        """
        Handle set_scene command, storing scene_id and optional subchannel_id,
        and publish assignment acknowledgment.
        """
        assignment_id = command.get("assignment_id", "set_scene")
        scene_id = command.get("scene_id")
        subchannel_id = command.get("subchannel_id")  # Optional

        self.logger.info(f"Handling set_scene: scene_id={scene_id}, subchannel_id={subchannel_id}, assignment_id={assignment_id}")

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
            self.logger.error(f"set_scene failed: {e}", exc_info=True)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="set_scene_failed",
                    message=str(e)
                )

    async def _handle_clear_scene(self, command: Dict[str, Any]):
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
            self.logger.error(f"clear_scene failed: {e}", exc_info=True)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="clear_scene_failed",
                    message=str(e)
                )

    async def _handle_refresh(self, command: Dict[str, Any]):
        """Handle refresh command - the API service should send display_image command with new content."""
        assignment_id = command.get("assignment_id", "refresh")
        self.logger.info(f"Received refresh command {assignment_id}")

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

        self.logger.info(f"Refresh acknowledged for scene {self.current_scene_id}. Waiting for API to send display_image command with fresh content.")

    def set_scene_callbacks(self, set_cb: Callable[[str, Optional[str]], Any], clear_cb: Callable[[], Any]):
        self._set_scene_cb = set_cb
        self._clear_scene_cb = clear_cb
    
    async def _handle_register_request(self, command: Dict[str, Any]):
        """Handle registration request from API service - send our details back."""
        self.logger.info(f"Received registration request from API: {command}")
        
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
            
            self.logger.info(f"Sending registration details to {reply_to}")
            self.logger.debug(f"Registration data: {registration_data}")
            
            # Send registration response via MQTT
            await self._mqtt_client.publish(
                reply_to,
                json.dumps(registration_data),
                qos=1
            )
            
            self.logger.info("Registration details sent successfully")
                
        except Exception as e:
            self.logger.error(f"Error handling registration request: {e}")
    
    async def _handle_ready(self, command: Dict[str, Any]):
        """Handle ready acknowledgment from API - display is registered and ready."""
        self.logger.info(f"Received ready acknowledgment: {command}")
        
        display_id = command.get("display_id")
        message = command.get("message", "Ready")
        
        self.logger.info(f"Display ready - ID: {display_id}, Message: {message}")
        
        # Send acknowledgment back
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "ready"),
                success=True,
                message="Ready acknowledgment received"
            )
    
    async def _handle_registration_complete(self, command: Dict[str, Any]):
        """Handle registration complete confirmation from API."""
        self.logger.info(f"Registration complete: {command}")
        
        display_id = command.get("display_id")
        message = command.get("message", "Registration complete")
        
        self.logger.info(f"Registration successful - ID: {display_id}, Message: {message}")
        
        # Send acknowledgment back
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "registration_complete"),
                success=True,
                message="Registration completion acknowledged"
            )
    
    

    def _pretty_display_target(self, image_url: str, max_len: int = 140) -> str:
        """
        Prefer the URL's basename if present; otherwise show the full URL (trimmed).
        """
        try:
            parsed = urlparse(image_url)
            # Try to show just the last path segment if it exists (nicer than a long URL)
            basename = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
            target = basename or image_url
        except Exception:
            target = image_url

        if len(target) > max_len:
            return target[: max_len - 1] + "…"
        return target

    async def _handle_display_image(self, command: Dict[str, Any]):
        """Handle display image command: download the image and render it to hardware."""
        self.logger.info("Received display image command: %s", command)

        assignment_id = command.get("assignment_id", "display_image")
        image_url = command.get("image_url")

        if not image_url:
            self.logger.error("Display image command missing image_url")
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="display_error",
                    message="Missing image_url in display command",
                )
            return

        # Immediate ACK so UI is responsive — show URL/basename instead of "Test Image"
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=assignment_id,
                success=True,
                message=f"Displaying: {self._pretty_display_target(image_url)}",
            )

        # Prefer the normal assignment pipeline (Downloader -> display_callback)
        if self.assignment_processor:
            start = datetime.now()
            try:
                pseudo = {
                    "assignment_id": assignment_id,
                    "content": {"delivery": {"type": "url", "url": image_url}},
                }
                result = await self.assignment_processor.process_assignment(pseudo)
                dur = int((datetime.now() - start).total_seconds() * 1000)

                if not result.get("success"):
                    # Second attempt: top-level delivery
                    pseudo_alt = {
                        "assignment_id": assignment_id,
                        "delivery": {"type": "url", "url": image_url},
                    }
                    result = await self.assignment_processor.process_assignment(pseudo_alt)

                if result.get("success"):
                    if self._event_publisher:
                        await self._event_publisher.publish_rendered(
                            assignment_id=assignment_id, duration_ms=dur
                        )
                    self.logger.info("Displayed image successfully in %dms", dur)
                    return
                else:
                    level = (
                        self.logger.info
                        if result.get("error_type") == "KeyError"
                        else self.logger.warning
                    )
                    level("Assignment processor returned failure, falling back: %s", result)

            except KeyError as e:
                # Classic signature mismatch (e.g., missing ['url'] where downloader expected it)
                self.logger.warning(
                    "process_assignment KeyError %s, falling back to direct download", e
                )
            except Exception as e:
                self.logger.exception(
                    "process_assignment failed (%s), falling back to direct download", e
                )

        # Fallback: direct async download → render
        try:
            start = datetime.now()
            path = await self._download_to_temp(image_url)
            await self._render_local_file(path)
            dur = int((datetime.now() - start).total_seconds() * 1000)

            if self._event_publisher:
                await self._event_publisher.publish_rendered(
                    assignment_id=assignment_id, duration_ms=dur
                )
            self.logger.info("Displayed image via fallback in %dms (path=%s)", dur, path)

            # Optional: send a follow-up info message with the local path (uncomment if you have such an event)
            # if self._event_publisher and hasattr(self._event_publisher, "publish_info"):
            #     await self._event_publisher.publish_info(
            #         assignment_id=assignment_id,
            #         message=f"Rendered local path: {path}",
            #     )

        except Exception as e:
            self.logger.exception("display_image fallback failed: %s", e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    assignment_id=assignment_id,
                    error_type="display_exception",
                    message=str(e),
                )


    async def _download_to_temp(self, url: str) -> Path:
        """Download URL to a temp file asynchronously with retries."""
        attempts = 3
        backoff = 0.75  # seconds
        async with ClientSession() as session:
            last_exc = None
            for i in range(1, attempts + 1):
                try:
                    async with session.get(url, timeout=ClientTimeout(total=20)) as resp:
                        # Retry on 5xx / 429
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
                    # jittered backoff
                    await asyncio.sleep(backoff * (1 + random.random() * 0.3))
                    backoff *= 1.6
            # All retries failed
            raise last_exc

    async def _render_local_file(self, path: Path) -> None:
        """Call the same display callback path the assignment processor uses."""
        # Reuse the same callback the manager provided to the MQTT client
        if self.assignment_processor and callable(self.assignment_processor.display_callback):
            await self.assignment_processor.display_callback(path, display_config={})
        else:
            # As an ultimate fallback, import the manager-level display directly if needed
            raise RuntimeError("Display callback unavailable; cannot render image")
