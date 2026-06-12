"""
Mimir Display Client Package

A comprehensive display client for the Mimir platform ecosystem, designed for
Raspberry Pi and e-ink displays.

Main Components:
- MqttDisplayClientManager: MQTT-based display client
- Network services: mDNS discovery and webhook server
- Content management: Image caching and display operations
- Hardware abstraction: Display hardware interfaces
"""

from .version import CLIENT_VERSION, PROTOCOL_VERSION

__version__ = CLIENT_VERSION  # single source of truth: package metadata / pyproject
__author__ = "Mimir Team"

from .config import Config
from .mqtt_client_manager import MqttDisplayClientManager

__all__ = ["MqttDisplayClientManager", "Config", "CLIENT_VERSION", "PROTOCOL_VERSION"]
