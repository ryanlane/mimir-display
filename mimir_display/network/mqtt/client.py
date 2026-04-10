import asyncio
import contextlib
import hashlib
import json
import logging
import os
import random
import socket
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from aiomqtt import Client

from .topics import MqttTopicManager
from .registration import MqttRegistrationManager
from .presence import MqttPresenceManager
from .events import MqttEventPublisher
from .commands import MqttCommandHandler

from mimir_display.config import Config
from mimir_display.content.downloader import ContentDownloader, AssignmentProcessor

class MqttDisplayClient:
    """Main MQTT client for display devices with registration state management and command processing."""
    
    def __init__(self, config: Config, capabilities: Dict[str, Any], metadata: Dict[str, Any], display_callback=None):
        
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Initialize device ID - use configured display_id (which falls back to hostname)
        self.device_id = config.display_id
        
        # Initialize components
        self.topics = MqttTopicManager(self.device_id)
        self.registration = MqttRegistrationManager(
            self.topics,
            capabilities,
            metadata,
        )

        # Initialize content processing
        self.downloader = ContentDownloader()
        self.assignment_processor = AssignmentProcessor(self.downloader, display_callback)

        # Initialize other managers with potentially updated topics
        self.presence = MqttPresenceManager(self.topics, config.mqtt_heartbeat_interval)
        self.events = MqttEventPublisher(self.topics)
        self.commands = MqttCommandHandler(self.topics, self.assignment_processor, capabilities, metadata, self.device_id, config.get('platform_url'))

        # Initialize scene management
        self._assigned_scene_id: Optional[str] = None
        self._assigned_subchannel_id: Optional[str] = None
        self._state_path = self._derive_state_path()  # e.g., <data_dir>/device_state.json
        # Extended local state (loaded in _load_local_state)
        self._assignment_meta: Dict[str, Any] = {}
        self._load_local_state()

        # Make sure presence starts with the scene_id and subchannel_id if we have them
        if self._assigned_scene_id:
            fields = {"scene_id": self._assigned_scene_id}
            if self._assigned_subchannel_id:
                fields["subchannel_id"] = self._assigned_subchannel_id
            self.presence.set_extra_fields(fields)
        

        # Check if we have a previous registration and use the assigned ID
        if self.registration.is_registered():
            effective_id = self.registration.get_effective_device_id()
            self.device_id = effective_id
            self.topics = MqttTopicManager(effective_id)
            self.logger.info("Using registered device ID: %s", effective_id)
        
               
        # Wire up event publisher to command handler
        self.commands.set_event_publisher(self.events)
        self.commands.set_presence_manager(self.presence)
        # after wiring event publisher & commands:
        self.commands.set_scene_callbacks(self.set_scene_id, self.clear_scene_id)
        self.commands.set_registration_manager(self.registration)

        # Pairing code (set externally before run_discovery_listener starts)
        self._pair_code: Optional[str] = None

        # Optional one-shot callback fired the first time MQTT connects successfully
        self._on_first_connect: Optional[Callable[[], None]] = None
        self._first_connect_fired = False
        self._on_pair_status: Optional[Callable[[str, Dict[str, Any]], None]] = None

        # State
        self._client: Optional[Client] = None
        self._running = False
        self._shutdown = False

        # Make sure presence starts with capabilities and the last known assignment.
        self._apply_presence_fields()

        # Resilience / watchdog configuration (pulled from Config; falls back to defaults)
        self._resilience = self._load_resilience_settings()

    def _presence_capability_fields(self) -> Dict[str, Any]:
        """Return capability hints that should accompany presence payloads."""
        capabilities = self.registration.capabilities or {}
        if not capabilities:
            return {}

        cap_payload: Dict[str, Any] = {}
        for key in (
            "resolution",
            "native_resolution",
            "orientation",
            "rotation_deg",
            "supported_formats",
            "redis_distribution",
            "content_claiming",
        ):
            value = capabilities.get(key)
            if value is not None:
                cap_payload[key] = value

        fields: Dict[str, Any] = {}
        if cap_payload:
            fields["cap"] = cap_payload

        resolution = capabilities.get("resolution")
        if resolution is not None:
            fields["res"] = resolution

        orientation = capabilities.get("orientation")
        if orientation:
            fields["orientation"] = orientation

        return fields

    def _apply_presence_fields(self) -> None:
        """Ensure status and heartbeat payloads expose capabilities and assignment state."""
        fields = self._presence_capability_fields()
        if self._assigned_scene_id:
            fields["scene_id"] = self._assigned_scene_id
        if self._assigned_subchannel_id:
            fields["subchannel_id"] = self._assigned_subchannel_id
        if fields:
            self.presence.set_extra_fields(fields)

    def _load_resilience_settings(self) -> Dict[str, Any]:
        """Load resilience configuration from config/env with sane defaults.

        Available (all optional):
          mqtt_reconnect_base_delay (int/float)  default 2s
          mqtt_reconnect_max_delay  (int/float)  default 60s
          mqtt_reconnect_jitter     (float 0..1) default 0.25 (fraction of delay)
          mqtt_reconnect_log_every  (int)        default 5 (emit full traceback every N attempts)
          mqtt_watchdog_interval    (int/float)  default 15s (how often watchdog samples idle time)
          mqtt_watchdog_idle_warn   (int/float)  default 45s (warn if no message/event for this long)
          mqtt_watchdog_idle_error  (int/float)  default 120s (error log if exceeded)
        """
        get = self.config.get

        # Helper to read env with fallbacks, allowing either env var or config key (lowercase form)
        def _num(env_key: str, cfg_key: str, default: float, cast):
            raw = os.getenv(env_key.upper())
            if raw is None:
                raw = get(cfg_key, default)
            try:
                return cast(raw)
            except Exception:  # noqa: BLE001 - defensive; fallback to default
                self.logger.warning(
                    "Invalid value for %s=%r falling back to default %.2f", env_key, raw, default
                )
                return default

        settings = {
            'base_delay': _num('MQTT_RECONNECT_BASE_DELAY', 'mqtt_reconnect_base_delay', 2.0, float),
            'max_delay': _num('MQTT_RECONNECT_MAX_DELAY', 'mqtt_reconnect_max_delay', 60.0, float),
            'jitter_frac': _num('MQTT_RECONNECT_JITTER', 'mqtt_reconnect_jitter', 0.25, float),
            'log_every': _num('MQTT_RECONNECT_LOG_EVERY', 'mqtt_reconnect_log_every', 5, int),
            'watchdog_interval': _num('MQTT_WATCHDOG_INTERVAL', 'mqtt_watchdog_interval', 15.0, float),
            'idle_warn': _num('MQTT_WATCHDOG_IDLE_WARN', 'mqtt_watchdog_idle_warn', 45.0, float),
            'idle_error': _num('MQTT_WATCHDOG_IDLE_ERROR', 'mqtt_watchdog_idle_error', 120.0, float),
        }

        # Clamp / sanity adjustments
        if settings['jitter_frac'] < 0 or settings['jitter_frac'] > 1:
            self.logger.warning(
                "mqtt_reconnect_jitter out of range (0..1) %.2f -> clamping", settings['jitter_frac']
            )
            settings['jitter_frac'] = max(0.0, min(1.0, settings['jitter_frac']))
        if settings['idle_warn'] >= settings['idle_error']:
            self.logger.warning(
                "MQTT idle warn %.1fs >= error %.1fs; adjusting warn to 75%% of error", settings['idle_warn'], settings['idle_error']
            )
            settings['idle_warn'] = settings['idle_error'] * 0.75
        if settings['watchdog_interval'] <= 0:
            self.logger.warning(
                "MQTT watchdog interval %.2f invalid; using 15s", settings['watchdog_interval']
            )
            settings['watchdog_interval'] = 15.0

        self.logger.info(
            "MQTT resilience config base=%.1fs max=%.1fs jitter=%.2f log_every=%d watchdog=%.1fs warn=%.1fs error=%.1fs",
            settings['base_delay'],
            settings['max_delay'],
            settings['jitter_frac'],
            settings['log_every'],
            settings['watchdog_interval'],
            settings['idle_warn'],
            settings['idle_error'],
        )
        return settings

    def _derive_state_path(self) -> str:
        # Try to use your configured data dir; fall back to /tmp
        base = getattr(self.config, "data_dir", None) or "/tmp/mimir_display"
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "device_state.json")
    
    def _load_local_state(self):
        """Load previously persisted assignment state (if any)."""
        try:
            if os.path.exists(self._state_path):  # noqa: BLE001 - defensive load
                with open(self._state_path, encoding="utf-8") as f:
                    data = json.load(f)
                self._assigned_scene_id = data.get("scene_id")
                self._assigned_subchannel_id = data.get("subchannel_id")
                # Preserve any meta fields (assignment_id, applied_at, version, etc.)
                self._assignment_meta = {
                    k: v for k, v in data.items() if k not in {"scene_id", "subchannel_id"}
                }
                self.logger.info(
                    "Loaded local assignment state: scene_id=%s subchannel_id=%s meta=%s",
                    self._assigned_scene_id,
                    self._assigned_subchannel_id,
                    self._assignment_meta or None,
                )
        except Exception as e:
            self.logger.debug("Failed to load local assignment state: %s", e, exc_info=True)  # noqa: BLE001

    def _persist_assignment_state(self):
        """Atomically persist current assignment state + metadata to disk."""
        payload = {
            "scene_id": self._assigned_scene_id,
            "subchannel_id": self._assigned_subchannel_id,
            **self._assignment_meta,
        }
        tmp_path = f"{self._state_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self._state_path)
        except Exception:
            self.logger.debug("Failed to persist device state", exc_info=True)  # noqa: BLE001
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:  # noqa: BLE001
                pass

    async def set_scene_id(
        self,
        scene_id: str,
        subchannel_id: Optional[str] = None,
        *,
        assignment_id: Optional[str] = None,
        source: str = "command",
    ):
        """Set (or update) the current scene assignment and republish status.

        Adds metadata: assignment_id, applied_at, source, version.
        Republish status only if changed (scene_id or subchannel_id differences) to reduce noise.
        """
        changed = (
            scene_id != self._assigned_scene_id
            or subchannel_id != self._assigned_subchannel_id
        )
        prev_scene = self._assigned_scene_id
        prev_sub = self._assigned_subchannel_id

        self._assigned_scene_id = scene_id
        self._assigned_subchannel_id = subchannel_id

        # Update metadata snapshot
        self._assignment_meta.update(
            {
                "assignment_id": assignment_id,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "version": 1,
            }
        )
        self._persist_assignment_state()

        fields = {"scene_id": scene_id}
        if subchannel_id is not None:
            fields["subchannel_id"] = subchannel_id
        self.presence.set_extra_fields(fields)

        if changed:
            await self.presence.publish_status()
            self.logger.info(
                "Assignment updated: scene_id=%s subchannel_id=%s (prev=%s/%s, assignment_id=%s)",
                scene_id,
                subchannel_id,
                prev_scene,
                prev_sub,
                assignment_id,
            )
        else:
            self.logger.debug(
                "Assignment unchanged; scene_id=%s subchannel_id=%s", scene_id, subchannel_id
            )

    async def clear_scene_id(self, *, assignment_id: Optional[str] = None, reason: str = "clear_command"):
        """Clear current scene assignment and republish status.

        Persists a cleared state with metadata for audit.
        """
        if self._assigned_scene_id is None and self._assigned_subchannel_id is None:
            self.logger.debug("clear_scene_id called but already unset; skipping publish")
            return

        prev_scene = self._assigned_scene_id
        prev_sub = self._assigned_subchannel_id
        self._assigned_scene_id = None
        self._assigned_subchannel_id = None
        self._assignment_meta.update(
            {
                "assignment_id": assignment_id,
                "cleared_at": datetime.now(timezone.utc).isoformat(),
                "clear_reason": reason,
                "version": 1,
            }
        )
        self._persist_assignment_state()
        self.presence.clear_extra("scene_id")
        self.presence.clear_extra("subchannel_id")
        await self.presence.publish_status()
        self.logger.info(
            "Assignment cleared (prev scene_id=%s subchannel_id=%s assignment_id=%s reason=%s)",
            prev_scene,
            prev_sub,
            assignment_id,
            reason,
        )

    def get_scene_id(self) -> Optional[str]:
        return self._assigned_scene_id

    def get_subchannel_id(self) -> Optional[str]:
        return getattr(self, "_assigned_subchannel_id", None)

    @asynccontextmanager
    async def connection(self):
        async with Client(
            hostname=self.config.mqtt_broker_host,
            port=self.config.mqtt_broker_port,
            identifier=self.device_id,
            username=self.config.mqtt_username,
            password=self.config.mqtt_password,
        ) as client:
            self._client = client
            self.events.set_client(client)

            self.logger.info(
                "Connected to MQTT broker at %s:%s", self.config.mqtt_broker_host, self.config.mqtt_broker_port
            )

            await self.presence.start_presence(client)

            try:
                yield client
            finally:
                # Best-effort offline while still connected
                try:
                    await self.presence.stop_presence()
                except Exception as e:
                    self.logger.debug(f"stop_presence failed: {e}")
                self.events.set_client(None)

        self.logger.info("MQTT connection closed")
    
    async def register(self) -> Optional[Dict[str, Any]]:
        """Register device with the service, updating state and topics as needed."""
        async with self.connection() as client:
            response = await self.registration.register_device(client)
            
            if response:
                # Update our device ID and topics if registration assigned a new ID
                new_device_id = self.registration.get_effective_device_id()
                if new_device_id != self.device_id:
                    self.device_id = new_device_id
                    self.topics = MqttTopicManager(self.device_id)
                    
                    # Update all components with new topics
                    self.events.topics = self.topics
                    self.commands.topics = self.topics
                    
                    # Restart presence with updated topics
                    await self.presence.stop_presence()
                    self.presence = MqttPresenceManager(self.topics, self.config.mqtt_heartbeat_interval)
                    self._apply_presence_fields()
                    self.commands.set_presence_manager(self.presence)
                    await self.presence.start_presence(client)
                    
                    self.logger.info("Registration complete - now using device ID: %s", self.device_id)
                
            return response
    
    def is_registered(self) -> bool:
        """Check if device is currently registered."""
        return self.registration.is_registered()
    
    def get_registration_summary(self) -> Dict[str, Any]:
        """Get registration status summary."""
        return self.registration.get_registration_summary()
    
    def clear_registration(self):
        """Clear registration state (force re-registration)."""
        old_id = self.device_id
        self.registration.clear_registration()
        
        # Reset to auto-generated device ID
        hostname = socket.gethostname()
        self.device_id = create_device_id(hostname)
        self.topics = MqttTopicManager(self.device_id)
        
        # Update all components
        self.events.topics = self.topics
        self.commands.topics = self.topics
        self.presence = MqttPresenceManager(self.topics, self.config.mqtt_heartbeat_interval)
        self._apply_presence_fields()
        self.commands.set_presence_manager(self.presence)
        self.logger.info("Registration cleared: %s -> %s", old_id, self.device_id)
    
    def set_display_callback(self, callback: Callable):
        """Set or update the display callback function."""
        self.assignment_processor.display_callback = callback
        self.logger.info("Display callback updated")
    
    def get_cache_info(self) -> Dict[str, Any]:
        """Get information about the content cache."""
        return self.downloader.get_cache_info()
    
    def clear_cache(self, keep_recent: int = 0) -> int:
        """Clear the content cache."""
        return self.downloader.clear_cache(keep_recent)
    
    def register_command_handler(self, command_type: str, handler: Callable):
        """Register a handler for MQTT commands."""
        self.commands.register_handler(command_type, handler)

    def set_pair_code(self, code: str) -> None:
        """Store the pairing code to be published on the next MQTT connection."""
        self._pair_code = code

    def set_on_first_connect(self, callback: Callable[[], None]) -> None:
        """Register a callback invoked once when MQTT first connects successfully."""
        self._on_first_connect = callback
        self._first_connect_fired = False

    def set_on_pair_status(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """Register a callback invoked when the server acks the pair code."""
        self._on_pair_status = callback

    async def request_reconnect(self, reason: str = "manual") -> None:
        """Request a reconnect by closing the current MQTT connection if present."""
        self.logger.info("MQTT reconnect requested (%s)", reason)
        client = self._client
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as e:  # noqa: BLE001
            self.logger.debug("MQTT disconnect failed: %s", e)
    
    async def run_discovery_listener(self):
        """Run the discovery command listener with automatic reconnect/backoff.

        This loop will continue attempting to connect to the MQTT broker.
        On connection-level failures it applies exponential backoff (capped)
        instead of crashing the whole task. Cancellation propagates immediately.

        Hardened features added:
          * Exponential backoff with jitter (configurable)
          * Structured logging of attempt, delay, reason
          * Watchdog monitoring idle gaps between handled messages/presence activity
          * Distinguishes keep-alive timeouts in logs for faster triage
          * Graceful shutdown via aclose()/shutdown flag
        """
        attempt = 0
        cfg = self._resilience

        while not self._shutdown:
            reason: Optional[str] = None
            # (removed disconnect_code variable – unused)
            try:
                async with self.connection() as client:
                    attempt = 0  # reset after successful connect
                    self.logger.info(
                        "Discovery listener connected device_id=%s broker=%s:%s",
                        self.device_id,
                        self.config.mqtt_broker_host,
                        self.config.mqtt_broker_port,
                    )
                    if not self._first_connect_fired and self._on_first_connect:
                        self._first_connect_fired = True
                        try:
                            self._on_first_connect()
                        except Exception as _cb_err:  # noqa: BLE001
                            self.logger.debug("on_first_connect callback error: %s", _cb_err)
                    self.commands.set_mqtt_client(client)

                    await client.subscribe(self.topics.commands, qos=1)
                    self.logger.info("Subscribed commands_topic=%s", self.topics.commands)

                    # Publish pair code so the server can store it for user claiming.
                    if self._pair_code:
                        try:
                            ack_topic = self.topics.pair_ack
                            await client.subscribe(ack_topic, qos=1)
                            payload = json.dumps({
                                "device_id": self.device_id,
                                "code": self._pair_code,
                                "capabilities": self.registration.capabilities,
                                "metadata": self.registration.metadata,
                                "reply_to": ack_topic,
                            })
                            await client.publish(
                                self.topics.pair_request(), payload, qos=1
                            )
                            self.logger.info(
                                "Pair request published code=%s ack_topic=%s",
                                self._pair_code, ack_topic,
                            )
                        except Exception as _pair_err:  # noqa: BLE001
                            self.logger.warning("Failed to publish pair request: %s", _pair_err)

                    # --- Watchdog setup ---
                    last_activity = time.monotonic()

                    async def watchdog():  # pragma: no cover - timing based
                        while True:
                            await asyncio.sleep(cfg['watchdog_interval'])
                            idle = time.monotonic() - last_activity
                            if idle > cfg['idle_error']:
                                self.logger.error(
                                    "MQTT idle gap exceeded error threshold idle=%.1fs warn=%.1fs error=%.1fs",
                                    idle,
                                    cfg['idle_warn'],
                                    cfg['idle_error'],
                                )
                            elif idle > cfg['idle_warn']:
                                self.logger.warning(
                                    "MQTT idle gap warning idle=%.1fs warn=%.1fs", idle, cfg['idle_warn']
                                )

                    watchdog_task = asyncio.create_task(watchdog(), name="mqtt.watchdog")

                    try:
                        async for message in client.messages:
                            last_activity = time.monotonic()  # activity mark
                            try:
                                topic_str = str(getattr(message, 'topic', ''))
                                if self._pair_code and topic_str == self.topics.pair_ack:
                                    # Pair ack received — log and clear so we don't re-publish
                                    try:
                                        ack_data = json.loads(message.payload)
                                    except Exception:
                                        ack_data = {}
                                    status = ack_data.get("status", "unknown")
                                    self.logger.info(
                                        "Pair ack received code=%s status=%s payload=%s",
                                        self._pair_code, status, ack_data,
                                    )
                                    if self._on_pair_status:
                                        try:
                                            self._on_pair_status(status, ack_data)
                                        except Exception as callback_error:  # noqa: BLE001
                                            self.logger.debug("pair status callback error: %s", callback_error)
                                    if status in ("ok", "pending"):
                                        # "pending" = server accepted and stored the code.
                                        # "ok" = legacy/alternative success signal.
                                        # Either way, stop re-publishing on reconnect — the
                                        # splash already shows the code and the server has it.
                                        self._pair_code = None
                                else:
                                    await self.commands.handle_command_message(message)
                            except Exception as e:  # noqa: BLE001 - includes CancelledError check
                                if isinstance(e, asyncio.CancelledError):
                                    raise
                                self.logger.error(
                                    "Command handling error topic=%s err=%s", getattr(message, 'topic', '?'), e,
                                    exc_info=True,
                                )
                    finally:
                        watchdog_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):  # type: ignore[name-defined]
                            await watchdog_task

            except asyncio.CancelledError:
                # Graceful cancellation: exit outer loop
                break
            except Exception as e:  # noqa: BLE001
                # Introspect for keep-alive hints (aiomqtt v5 code 141) without tight coupling
                es = str(e)
                if '141' in es and 'Keep alive' in es:
                    reason = 'keepalive_timeout'
                elif 'ConnectionRefusedError' in es:
                    reason = 'conn_refused'
                else:
                    reason = 'error'

                attempt += 1
                full_tb = (attempt == 1) or (attempt % cfg['log_every'] == 0)
                log_msg = (
                    f"MQTT reconnect needed attempt={attempt} reason={reason} device_id={self.device_id} "
                    f"host={self.config.mqtt_broker_host} port={self.config.mqtt_broker_port}"
                )
                if full_tb:
                    self.logger.error("%s error=%r", log_msg, e, exc_info=True)
                else:
                    self.logger.warning("%s error=%r", log_msg, e)

                # Ensure command handler releases client
                with contextlib.suppress(Exception):  # type: ignore[name-defined]
                    self.commands.set_mqtt_client(None)

                # Backoff with jitter
                base = cfg['base_delay']
                max_d = cfg['max_delay']
                delay = min(base * (2 ** (attempt - 1)), max_d)
                jitter_frac = max(0.0, min(1.0, cfg['jitter_frac']))
                jitter = delay * jitter_frac
                sleep_for = delay + random.uniform(-jitter, jitter)
                # Clamp to >= 0.25s
                sleep_for = max(0.25, sleep_for)
                self.logger.info(
                    "MQTT reconnect backoff sleeping=%.2fs attempt=%d base=%.1f max=%.1f", sleep_for, attempt, base, max_d
                )
                try:
                    await asyncio.sleep(sleep_for)
                except asyncio.CancelledError:
                    raise
                continue
            finally:
                # Connection context exited (graceful or error) => clear client ref
                with contextlib.suppress(Exception):  # type: ignore[name-defined]
                    self.commands.set_mqtt_client(None)

            # Clean (non-exception) exit path -> likely broker initiated disconnect
            if not self._shutdown:
                self.logger.info("MQTT connection closed cleanly; reconnecting shortly")
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise

        self.logger.info("Discovery listener exiting (shutdown flag set)")



    async def run_command_listener(self):
        """Run the command listener loop."""
        async with self.connection() as client:
            await self.commands.start_listening(client)
    
    async def publish_event(self, event_type: str, **kwargs):
        """Convenience method to publish events."""
        if event_type == "ack":
            await self.events.publish_ack(**kwargs)
        elif event_type == "rendered":
            await self.events.publish_rendered(**kwargs)
        elif event_type == "error":
            await self.events.publish_error(**kwargs)
        else:
            self.logger.warning(f"Unknown event type: {event_type}")

    async def aclose(self):
        """Public async close to signal shutdown and close active connection.

        Idempotent. Sets shutdown flag so outer loops cease reconnecting.
        """
        if self._shutdown:
            return
        self._shutdown = True
        try:
            if self._client is not None:
                # Best effort: rely on context manager exit to close.
                with contextlib.suppress(Exception):  # type: ignore[name-defined]
                    await asyncio.sleep(0)  # yield so loops can observe flag
        finally:
            self.logger.info("MqttDisplayClient shutdown flag set")


def create_device_id(hostname: str) -> str:
    """Create a consistent device ID from hostname."""
    return f"auto-{hostname}"


def create_display_id_hash(device_id: str) -> str:
    """Create a display ID hash for service assignment."""
    return f"display-{hashlib.md5(device_id.encode()).hexdigest()[:6]}"