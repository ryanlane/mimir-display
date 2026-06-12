"""
MQTT Client for Mimir Display Client

This module provides MQTT communication capabilities for the display client,
implementing the topic hierarchy and message patterns defined in the migration plan.

Topic Hierarchy:
- mimir/<id>/status     - Device presence (online/offline with LWT)
- mimir/<id>/heartbeat  - Periodic heartbeat messages
- mimir/<id>/evt        - Device → service events (ack, rendered, error)
- mimir/<id>/cmd        - Service → device commands (assign, refresh)
- mimir/registry/register - Device registration requests
"""
