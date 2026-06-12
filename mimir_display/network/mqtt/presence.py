from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable

from aiomqtt import Client

from .topics import MqttTopicManager


class MqttPresenceManager:
    """Manages device presence via MQTT status and heartbeat."""

    def __init__(
        self,
        topics: MqttTopicManager,
        heartbeat_interval: int = 30,
        activity_callback: Callable[[str], None] | None = None,
    ):
        self.topics = topics
        self.heartbeat_interval = heartbeat_interval
        self.logger = logging.getLogger(__name__)
        self._heartbeat_task: asyncio.Task | None = None
        self._client: Client | None = None
        self._extra_fields: dict[str, Any] = {}
        self._activity_callback = activity_callback

    def set_extra_fields(self, fields: dict[str, Any]):
        """Merge extra fields (e.g., {'scene_id': 'abc123'}) into status/heartbeat."""
        self._extra_fields.update(fields or {})
        self.logger.debug("Updated extra fields: %s", self._extra_fields)

    def _merge_extra(self, base: dict[str, Any]) -> dict[str, Any]:
        out = dict(base)
        for key, value in (self._extra_fields or {}).items():
            if value is not None:
                out[key] = value
        return out

    def clear_extra(self, key: str):
        """Remove a key from extra fields."""
        self._extra_fields.pop(key, None)

    def _note_activity(self, reason: str) -> None:
        if self._activity_callback:
            try:
                self._activity_callback(reason)
            except Exception as exc:
                self.logger.debug("Presence activity callback failed (%s): %s", reason, exc)

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
        self._note_activity("presence_status")
        self.logger.info("Republished online status to %s", self.topics.status)

    async def start_presence(self, client: Client):
        """Start presence management with online status and heartbeat."""
        self._client = client
        online_payload = self._merge_extra({
            "status": "online",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "device_id": self.topics.device_id,
            "hostname": socket.gethostname(),
            "heartbeat_interval": self.heartbeat_interval,
        })
        await client.publish(self.topics.status, json.dumps(online_payload), qos=1, retain=True)
        self._note_activity("presence_online")
        self.logger.info("Published online status to %s", self.topics.status)
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop_presence(self, reason: str = "graceful_shutdown"):
        """Stop heartbeat and publish retained offline status (best-effort, idempotent)."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            finally:
                self._heartbeat_task = None

        if self._client:
            try:
                offline_payload = self._merge_extra({
                    "status": "offline",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "device_id": self.topics.device_id,
                    "reason": reason,
                })
                await self._client.publish(self.topics.status, json.dumps(offline_payload), qos=1, retain=True)
                self._note_activity("presence_offline")
                self.logger.info("Published offline status: %s", reason)
            except Exception as exc:
                self.logger.debug("Unable to publish offline status: %s", exc)

        self._client = None

    async def _heartbeat_loop(self):
        """Send periodic heartbeat messages."""
        try:
            while True:
                heartbeat_payload = self._merge_extra({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "device_id": self.topics.device_id,
                    "uptime": time.time(),
                })
                self.logger.debug("Publishing heartbeat: %s", heartbeat_payload)
                await self._client.publish(self.topics.heartbeat, json.dumps(heartbeat_payload), qos=0)
                self._note_activity("presence_heartbeat")
                self.logger.debug("Heartbeat sent at %s", datetime.now().strftime("%H:%M:%S"))
                await asyncio.sleep(self.heartbeat_interval)
        except asyncio.CancelledError:
            self.logger.debug("Heartbeat loop cancelled")
        except Exception as exc:
            self.logger.error("Heartbeat loop error: %s", exc)
