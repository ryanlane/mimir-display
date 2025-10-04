import json
import asyncio
import socket
import logging
import time
from typing import Dict, Optional, Any
from aiomqtt import Client
from datetime import datetime, timezone
from .topics import MqttTopicManager

class MqttPresenceManager:
    """Manages device presence via MQTT status and heartbeat."""
    
    def __init__(self, topics: MqttTopicManager, heartbeat_interval: int = 30):
        self.topics = topics
        self.heartbeat_interval = heartbeat_interval
        self.logger = logging.getLogger(__name__)
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._client: Optional[Client] = None
        self._extra_fields: Dict[str, Any] = {} 
    
    def set_extra_fields(self, fields: Dict[str, Any]):
        """Merge extra fields (e.g., {'scene_id': 'abc123'}) into status/heartbeat."""
        self._extra_fields.update(fields or {})
        self.logger.debug(f"Updated extra fields: {self._extra_fields}")

    def _merge_extra(self, base: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(base)
        for k, v in (self._extra_fields or {}).items():
            if v is not None:
                out[k] = v
        return out

    def clear_extra(self, key: str):
        """Remove a key from extra fields."""
        self._extra_fields.pop(key, None)    

    async def publish_status(self):
        """Republish retained online status with current extra fields (e.g., after assignment)."""
        if not self._client:
            return
        payload = self._merge_extra({
            "status": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self.topics.device_id,
            "hostname": socket.gethostname(),
            "heartbeat_interval": self.heartbeat_interval,
        })
        await self._client.publish(self.topics.status, json.dumps(payload), qos=1, retain=True)
        self.logger.info(f"Republished online status to {self.topics.status}")

    async def start_presence(self, client: Client):
        """Start presence management with online status and heartbeat."""
        self._client = client

        # Initial retained "online" (now with extra fields)
        online_payload = self._merge_extra({
            "status": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self.topics.device_id,
            "hostname": socket.gethostname(),
            "heartbeat_interval": self.heartbeat_interval
        })
        await client.publish(self.topics.status, json.dumps(online_payload), qos=1, retain=True)
        self.logger.info(f"Published online status to {self.topics.status}")

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_presence(self, reason: str = "graceful_shutdown"):
        """Stop heartbeat and publish retained offline status (best-effort, idempotent)."""
        # Cancel the heartbeat loop
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            finally:
                self._heartbeat_task = None

        # Publish retained offline status while the client is still connected
        if self._client:
            try:
                offline_payload = self._merge_extra({
                    "status": "offline",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "device_id": self.topics.device_id,
                    "reason": reason,
                })
                await self._client.publish(self.topics.status, json.dumps(offline_payload), qos=1, retain=True)
                self.logger.info(f"Published offline status: {reason}")
            except Exception as e:
                # Don’t crash shutdown if broker is already gone
                self.logger.debug(f"Unable to publish offline status: {e}")

        # Detach reference either way
        self._client = None

    async def _heartbeat_loop(self):
        """Send periodic heartbeat messages."""
        try:
            while True:
                heartbeat_payload = self._merge_extra({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "device_id": self.topics.device_id,
                    "uptime": time.time()
                })
                self.logger.debug(f"Publishing heartbeat: {heartbeat_payload}")
                await self._client.publish(self.topics.heartbeat, json.dumps(heartbeat_payload), qos=0)
                self.logger.debug(f"Heartbeat sent at {datetime.now().strftime('%H:%M:%S')}")
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            self.logger.debug("Heartbeat loop cancelled")
        except Exception as e:
            self.logger.error(f"Heartbeat loop error: {e}")
