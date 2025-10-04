import json
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Any
from .topics import MqttTopicManager


from aiomqtt import Client

class MqttEventPublisher:
    """Publishes device events to the service."""
    
    def __init__(self, topics: MqttTopicManager):
        self.topics = topics
        self.logger = logging.getLogger(__name__)
        self._client: Optional[Client] = None
    
    def set_client(self, client: Client):
        """Set the MQTT client for publishing."""
        self._client = client
    
    async def publish_ack(
        self,
        assignment_id: Optional[str] = None,
        sequence: Optional[int] = None,
        success: bool = True,
        message: Optional[str] = None,
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
    
    async def publish_rendered(self, assignment_id: str, duration_ms: Optional[int] = None):
        """Publish successful rendering event."""
        payload = {
            "type": "rendered",
            "assignment_id": assignment_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms
        }
        
        await self._publish_event(payload)
    
    async def publish_error(self, assignment_id: Optional[str] = None, error_type: str = "unknown", message: str = ""):
        """Publish error event."""
        payload = {
            "type": "error",
            "assignment_id": assignment_id,
            "when": error_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        await self._publish_event(payload)
    
    async def _publish_event(self, payload: Dict[str, Any]):
        """Publish event to the events topic."""
        if not self._client:
            self.logger.error("No MQTT client available for event publishing")
            return
        
        await self._client.publish(self.topics.events, json.dumps(payload), qos=0)
        self.logger.debug(f"Published event: {payload['type']}")
