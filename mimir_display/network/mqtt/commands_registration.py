from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .topics import MqttTopicManager

if TYPE_CHECKING:
    from aiomqtt import Client

    from .events import MqttEventPublisher


class RegistrationCommandHandler:
    """Handles MQTT commands related to device registration and lifecycle.

    Commands: register, ready, registration_complete, finalize_registration, update_client
    """

    def __init__(
        self,
        topics: MqttTopicManager,
        capabilities: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        display_id: str | None = None,
        platform_url: str | None = None,
    ):
        self.topics = topics
        self.capabilities = capabilities or {}
        self.metadata = metadata or {}
        self.display_id = display_id
        self.platform_url = platform_url
        self.logger = logging.getLogger(__name__)
        self._event_publisher: MqttEventPublisher | None = None
        self._mqtt_client: Client | None = None
        self._registration: Any | None = None

    def set_event_publisher(self, event_publisher: MqttEventPublisher) -> None:
        self._event_publisher = event_publisher

    def set_mqtt_client(self, client: Client | None) -> None:
        self._mqtt_client = client

    def set_registration_manager(self, registration: Any) -> None:
        self._registration = registration

    async def handle_register(self, command: dict[str, Any]) -> None:
        """Handle registration request from API service — send device details back."""
        self.logger.info("Received registration request from API: %s", command)

        reply_to = command.get("reply_to")
        if not reply_to:
            self.logger.error("Registration request missing reply_to topic")
            return

        if not self._mqtt_client:
            self.logger.error("No MQTT client available for sending registration response")
            return

        try:
            registration_data = {
                "device_id": self.topics.device_id,
                "capabilities": self.capabilities,
                "metadata": self.metadata,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self.logger.info("Sending registration details to %s", reply_to)
            self.logger.debug("Registration data: %s", registration_data)
            await self._mqtt_client.publish(reply_to, json.dumps(registration_data), qos=1)
            self.logger.info("Registration details sent successfully")
        except Exception as e:  # broad: network / serialization errors
            self.logger.error("Error handling registration request: %s", e)

    async def handle_ready(self, command: dict[str, Any]) -> None:
        """Handle ready acknowledgment from API — device confirms it is operational.

        This command does not currently trigger any display action; it simply acknowledges
        registration/handshake steps. Additional fields in the payload are ignored but logged.
        """
        self.logger.info("Received ready acknowledgment: %s", command)
        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "ready"),
                success=True,
                message=command.get("message", "Ready acknowledgment received"),
            )

    async def handle_registration_complete(self, command: dict[str, Any]) -> None:
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

    async def handle_finalize_registration(self, command: dict[str, Any]) -> None:
        """Handle finalize_registration sent by the server after a pairing claim.

        The pairing flow is:
          Display publishes pair request → server stores code → user enters code in UI
          → server creates DB record → server sends finalize_registration to display/cmd

        This is the 'you have been claimed and registered' signal.  We persist
        the assigned display_id so is_registered() returns True on next start.
        """
        display_id: str | None = command.get("display_id")
        registration_key: str | None = command.get("registration_key")
        self.logger.info("Received finalize_registration display_id=%s", display_id)

        if self._registration is not None:
            try:
                self._registration.update_registration(
                    device_id=self.topics.device_id,
                    assigned_id=display_id or self.topics.device_id,
                    service_config={"registration_key": registration_key} if registration_key else {},
                )
                self.logger.info("Registration state persisted for display_id=%s", display_id)
            except Exception as e:
                self.logger.warning("Could not persist registration state: %s", e)

        try:
            from mimir_display.storage.device_config import DeviceConfig
            dc = DeviceConfig()
            dc.apply_finalize_payload(command)
        except Exception as e:
            self.logger.warning("Could not persist device config: %s", e)

        if self._event_publisher:
            await self._event_publisher.publish_ack(
                assignment_id=command.get("assignment_id", "finalize_registration"),
                success=True,
                message=f"Pairing complete — display_id={display_id}",
            )

    async def handle_update_client(self, command: dict[str, Any]) -> None:
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
