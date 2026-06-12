from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from aiomqtt import Client

from .topics import MqttTopicManager


class MqttEventPublisher:
    """Publishes device events to the service."""

    def __init__(self, topics: MqttTopicManager):
        self.topics = topics
        self.logger = logging.getLogger(__name__)
        self._client: Client | None = None

    def set_client(self, client: Client):
        """Set the MQTT client for publishing."""
        self._client = client

    async def publish_ack(
        self,
        assignment_id: str | None = None,
        sequence: int | None = None,
        success: bool = True,
        message: str | None = None,
        **extra,  # tolerate future fields without crashing
    ):
        """Publish assignment acknowledgment."""
        payload = {
            "type": "ack",
            "assignment_id": assignment_id,
            "sequence": sequence,
            "ok": success,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if message is not None:
            payload["message"] = message
        if extra:
            payload.update(extra)

        await self._publish_event(payload)

    async def publish_rendered(self, assignment_id: str, duration_ms: int | None = None):
        """Publish successful rendering event."""
        payload = {
            "type": "rendered",
            "assignment_id": assignment_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms
        }

        await self._publish_event(payload)

    async def publish_error(self, assignment_id: str | None = None, error_type: str = "unknown", message: str = ""):
        """Publish error event."""
        payload = {
            "type": "error",
            "assignment_id": assignment_id,
            "when": error_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        await self._publish_event(payload)

    async def _publish_event(self, payload: dict[str, Any]):
        """Publish event to the events topic."""
        if not self._client:
            self.logger.error("No MQTT client available for event publishing")
            return

        await self._client.publish(self.topics.events, json.dumps(payload), qos=0)
        self.logger.debug("Published event: %s", payload["type"])
