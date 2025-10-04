"""
Configuration management for Mimir Display Client.

This module handles all configuration loading, validation, and management
for the display client, including environment variables, CLI arguments,
and persistent settings.
"""

import os
import socket
from typing import Dict, List, Any, Optional
from .utils.helpers import env_str, env_int, env_float


class Config:
    """
    Configuration manager for the display client.
    
    Handles loading configuration from multiple sources:
    1. Environment variables (highest priority)
    2. Command-line arguments (during registration)
    3. Default values (lowest priority)
    """
    
    def __init__(self, args=None):
        """
        Initialize configuration from environment and CLI args.
        
        Args:
            args: Command-line arguments namespace (optional)
        """
        self.args = args
        self._config = self._load_base_config()
        
        # Apply CLI overrides if provided
        if args:
            self._apply_cli_overrides()
    
    def _load_base_config(self) -> Dict[str, Any]:
        """Load base configuration from environment variables."""
        return {
            "platform_url": os.getenv("PLATFORM_URL", "http://localhost:5000"),
            "display_id": os.getenv("DISPLAY_ID", ""),  # Empty means use hostname
            "display_name": os.getenv("DISPLAY_NAME", "Inky Display"),
            "display_location": os.getenv("DISPLAY_LOCATION", "Unknown"),
            "hostname": os.getenv("HOSTNAME", socket.gethostname()),
            "tags": [t.strip() for t in os.getenv("DISPLAY_TAGS", "").split(",") if t.strip()],
            "client_version": os.getenv("CLIENT_VERSION", "1.0.0"),
            "poll_interval": env_int("POLL_INTERVAL_SECONDS", 30),
            "retry_attempts": env_int("RETRY_ATTEMPTS", 3),
            "retry_delay": env_int("RETRY_DELAY_SECONDS", 5),
            "cache_timeout_seconds": env_int("CACHE_TIMEOUT_SECONDS", 300),
            "default_content_path": os.getenv("DEFAULT_CONTENT_PATH", ""),
            "assignment_ttl_seconds": env_int("ASSIGNMENT_TTL_SECONDS", 300),
            "auth_token": os.getenv("AUTH_TOKEN", ""),
            
            # Network configuration
            "webhook_port": env_int("WEBHOOK_PORT", 8081),
            "webhook_enabled": env_str("WEBHOOK_ENABLED", "true").lower() == "true",
            
            # MQTT configuration
            "mqtt_broker_host": os.getenv("MQTT_BROKER_HOST", "localhost"),
            "mqtt_broker_port": env_int("MQTT_BROKER_PORT", 1883),
            "mqtt_username": os.getenv("MQTT_USERNAME"),
            "mqtt_password": os.getenv("MQTT_PASSWORD"),
            "mqtt_heartbeat_interval": env_int("MQTT_HEARTBEAT_INTERVAL", 30),
            
            # Operational modes
            "discovery_mode": env_str("DISCOVERY_MODE", "false").lower() == "true",
            "use_redis_distribution": env_str("USE_REDIS_DISTRIBUTION", "true").lower() == "true",
            
            # Logging
            "log_level": os.getenv("LOG_LEVEL", "INFO"),
            "data_dir": os.getenv("DATA_DIR", ""),  # Will be set by client if empty
        }
    
    def _apply_cli_overrides(self):
        """Apply command-line argument overrides."""
        override_fields = [
            "platform_url", "display_id", "display_name", "display_location", 
            "hostname", "tags", "client_version", "default_content_path"
        ]
        
        for field in override_fields:
            value = getattr(self.args, field, None)
            if value is not None:
                if field == "tags" and isinstance(value, str):
                    # Handle comma-separated tag string
                    self._config[field] = [t.strip() for t in value.split(",") if t.strip()]
                else:
                    self._config[field] = value
    
    def get(self, key: str, default=None):
        """Get configuration value by key."""
        return self._config.get(key, default)
    
    def set(self, key: str, value: Any):
        """Set configuration value."""
        self._config[key] = value
    
    def update(self, updates: Dict[str, Any]):
        """Update multiple configuration values."""
        self._config.update(updates)
    
    def to_dict(self) -> Dict[str, Any]:
        """Return configuration as dictionary."""
        return self._config.copy()
    
    @property
    def platform_url(self) -> str:
        return self._config["platform_url"]
    
    @property
    def display_name(self) -> str:
        return self._config["display_name"]
    
    @property
    def display_location(self) -> str:
        return self._config["display_location"]
    
    @property
    def hostname(self) -> str:
        return self._config["hostname"]
    
    @property
    def tags(self) -> List[str]:
        return self._config["tags"]
    
    @property
    def webhook_enabled(self) -> bool:
        return self._config["webhook_enabled"]
    
    @property
    def webhook_port(self) -> int:
        return self._config["webhook_port"]
    
    @property
    def discovery_mode(self) -> bool:
        return self._config["discovery_mode"]
    
    @property
    def use_redis_distribution(self) -> bool:
        return self._config["use_redis_distribution"]
    
   
    @property
    def mqtt_broker_host(self) -> str:
        return self._config["mqtt_broker_host"]
    
    @property
    def mqtt_broker_port(self) -> int:
        return self._config["mqtt_broker_port"]
    
    @property
    def mqtt_username(self) -> Optional[str]:
        return self._config["mqtt_username"]
    
    @property
    def mqtt_password(self) -> Optional[str]:
        return self._config["mqtt_password"]
    
    @property
    def mqtt_heartbeat_interval(self) -> int:
        return self._config["mqtt_heartbeat_interval"]
    
    @property
    def display_id(self) -> str:
        """Get effective display ID - use configured value or fall back to hostname."""
        configured_id = self._config["display_id"]
        return configured_id if configured_id else self._config["hostname"]
