from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from .topics import MqttTopicManager
from aiomqtt import Client

from mimir_display.storage.registration import RegistrationState

class MqttRegistrationManager:
    """Handles device registration via MQTT with persistent state."""
    
    def __init__(self, topics: MqttTopicManager, capabilities: dict[str, Any], metadata: dict[str, Any]):
        self.topics = topics
        self.capabilities = capabilities
        self.metadata = metadata
        self.logger = logging.getLogger(__name__)
        self.state = RegistrationState()
        self._registration_response: dict[str, Any] | None = None
    
    def is_registered(self) -> bool:
        """Check if device is currently registered with valid state."""
        return self.state.is_registered
    
    def get_effective_device_id(self) -> str:
        """Get the device ID to use for MQTT topics (assigned_id if available)."""
        return self.state.assigned_id or self.topics.device_id
    
    async def register_device(self, client: Client) -> dict[str, Any] | None:
        """Register device and wait for response, updating persistent state."""
        # Subscribe to registration reply
        await client.subscribe(self.topics.registration_reply, qos=1)
        self.logger.info("Subscribed to registration replies at %s", self.topics.registration_reply)
        
        # Send registration request
        registration_payload = {
            "device_id": self.topics.device_id,
            "capabilities": self.capabilities,
            "metadata": self.metadata,
            "reply_to": self.topics.registration_reply,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        await client.publish(
            self.topics.registry(), 
            json.dumps(registration_payload), 
            qos=1
        )
        self.logger.info("Sent registration request for device %s", self.topics.device_id)
        
        # Wait for response (with timeout)
        try:
            # Use asyncio.wait_for for compatibility instead of asyncio.timeout
            response = await asyncio.wait_for(
                self._wait_for_registration_response(client),
                timeout=30.0
            )
            
            if response:
                # Update persistent state
                assigned_id = response.get('assigned_id')
                service_config = response.get('config', {})
                
                if assigned_id:
                    self.state.update_registration(
                        device_id=self.topics.device_id,
                        assigned_id=assigned_id,
                        service_config=service_config
                    )
                    
                    # Update topics to use assigned ID
                    self.topics.device_id = assigned_id
                    self.topics.base = f"mimir/{assigned_id}"
                    
                    self.logger.info("Registration successful: %s", assigned_id)
                    return response
                else:
                    self.logger.error("Registration response missing assigned_id")
                    return None
            
        except asyncio.TimeoutError:
            self.logger.error("Registration timeout - no response received")
            return None
    
    async def _wait_for_registration_response(self, client: Client) -> dict[str, Any] | None:
        """Wait for registration response message."""
        async for message in client.messages:
            if message.topic.value == self.topics.registration_reply:
                try:
                    response = json.loads(message.payload.decode())
                    self.logger.info("Received registration response: %s", response)
                    return response
                except json.JSONDecodeError as e:
                    self.logger.error("Invalid registration response JSON: %s", e)
                    continue
        return None
    
    def clear_registration(self):
        """Clear registration state (force re-registration)."""
        self.state.clear_registration()
        self.logger.info("Registration state cleared")
    
    def get_registration_summary(self) -> dict[str, Any]:
        """Get summary of current registration status."""
        return self.state.get_state_summary()
