from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from mimir_display.content.downloader import AssignmentProcessor
from .topics import MqttTopicManager
from .commands_registration import RegistrationCommandHandler
from .commands_display import DisplayCommandHandler

if TYPE_CHECKING:
    from .events import MqttEventPublisher
    from .presence import MqttPresenceManager
    from aiomqtt import Client

__all__ = ["MqttCommandHandler", "RegistrationCommandHandler", "DisplayCommandHandler"]


class MqttCommandHandler:
    """Thin dispatcher that routes incoming MQTT commands to domain-focused handlers.

    Owns the command registry and dispatch loop. Delegates all business logic to
    RegistrationCommandHandler (register/ready/finalize/update) and
    DisplayCommandHandler (assign/display_image/set_scene/clear_scene/refresh).
    """

    def __init__(
        self,
        topics: MqttTopicManager,
        assignment_processor: AssignmentProcessor | None = None,
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        display_id: str | None = None,
        platform_url: str | None = None,
    ):
        self._topics = topics
        self._capabilities = capabilities or {}
        self.logger = logging.getLogger(__name__)
        self.command_handlers: dict[str, Callable] = {}
        self._event_publisher: MqttEventPublisher | None = None
        self._mqtt_client: Client | None = None

        self._registration_handler = RegistrationCommandHandler(
            topics, capabilities, metadata, display_id, platform_url
        )
        self._display_handler = DisplayCommandHandler(topics, assignment_processor)

        # Registration commands
        self.register_handler("register", self._registration_handler.handle_register)
        self.register_handler("ready", self._registration_handler.handle_ready)
        self.register_handler("registration_complete", self._registration_handler.handle_registration_complete)
        self.register_handler("finalize_registration", self._registration_handler.handle_finalize_registration)
        self.register_handler("update_client", self._registration_handler.handle_update_client)

        # Display commands
        self.register_handler("assign", self._display_handler.handle_assign)
        self.register_handler("refresh", self._display_handler.handle_refresh)
        self.register_handler("display_image", self._display_handler.handle_display_image)
        self.register_handler("set_scene", self._display_handler.handle_set_scene)
        self.register_handler("clear_scene", self._display_handler.handle_clear_scene)

    # ------------------------------------------------------------------
    # Properties — propagate attribute updates to sub-handlers
    # ------------------------------------------------------------------

    @property
    def topics(self) -> MqttTopicManager:
        return self._topics

    @topics.setter
    def topics(self, value: MqttTopicManager) -> None:
        self._topics = value
        self._registration_handler.topics = value
        self._display_handler.topics = value

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._capabilities

    @capabilities.setter
    def capabilities(self, value: dict[str, Any]) -> None:
        self._capabilities = value
        self._registration_handler.capabilities = value

    # ------------------------------------------------------------------
    # Wiring setters — delegate to sub-handlers as appropriate
    # ------------------------------------------------------------------

    def set_event_publisher(self, event_publisher: MqttEventPublisher) -> None:
        """Set the event publisher for sending responses."""
        self._event_publisher = event_publisher
        self._registration_handler.set_event_publisher(event_publisher)
        self._display_handler.set_event_publisher(event_publisher)

    def set_presence_manager(self, presence_manager: MqttPresenceManager) -> None:
        """Set the presence manager for updating heartbeat fields."""
        self._display_handler.set_presence_manager(presence_manager)

    def set_mqtt_client(self, client: Client | None) -> None:
        """Set the MQTT client for sending registration responses."""
        self._mqtt_client = client
        self._registration_handler.set_mqtt_client(client)
        if self._event_publisher:
            self._event_publisher.set_client(client)

    def set_registration_manager(self, registration: Any) -> None:
        """Provide access to MqttRegistrationManager so finalize_registration can persist state."""
        self._registration_handler.set_registration_manager(registration)

    def set_scene_callbacks(
        self,
        set_cb: Callable[[str, str | None], Any],
        clear_cb: Callable[[], Any],
    ) -> None:
        self._display_handler.set_scene_callbacks(set_cb, clear_cb)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def register_handler(self, command_type: str, handler: Callable) -> None:
        """Register a handler for a specific command type."""
        self.command_handlers[command_type] = handler
        self.logger.info("Registered handler for command type: %s", command_type)

    async def handle_command_message(self, message) -> None:
        """Handle a raw MQTT command message."""
        if message.topic.value == self._topics.commands:
            await self._handle_command(message.payload.decode())

    async def start_listening(self, client: Client) -> None:
        """Start listening for commands."""
        self.set_mqtt_client(client)
        await client.subscribe(self._topics.commands, qos=1)
        self.logger.info("Subscribed to commands at %s", self._topics.commands)

        async for message in client.messages:
            if message.topic.value == self._topics.commands:
                await self._handle_command(message.payload.decode())

    async def _handle_command(self, payload: str) -> None:
        """Parse and dispatch an incoming command payload."""
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
                if self._event_publisher:
                    await self._event_publisher.publish_error(
                        assignment_id=assignment_id,
                        error_type="unknown_command",
                        message=f"No handler for command type: {command_type}",
                    )

        except json.JSONDecodeError as e:
            self.logger.error("Invalid command JSON: %s", e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    error_type="invalid_json",
                    message=f"Command payload is not valid JSON: {e}",
                )
        except Exception as e:
            self.logger.error("Error handling command: %s", e)
            if self._event_publisher:
                await self._event_publisher.publish_error(
                    error_type="command_processing",
                    message=f"Command processing failed: {e}",
                )
