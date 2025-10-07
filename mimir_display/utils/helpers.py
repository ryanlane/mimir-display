#!/usr/bin/env python3
"""
Utility functions for the Mimir Display Client.

This module contains common utility functions that are used throughout
the display client application, including:

- Environment variable parsing helpers
- URL manipulation utilities
- File system utilities
- Time and date parsing functions
- Hash calculation utilities
- Logging configuration

These utilities are extracted to keep the main display client module
focused on core display functionality.
"""

import hashlib
import logging
import os
import sys
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse


def env_str(key: str, default: Optional[str] = None) -> str:
    """
    Get a string value from environment variables.
    
    Args:
        key: Environment variable name
        default: Default value if not found
        
    Returns:
        String value from environment
        
    Raises:
        RuntimeError: If required variable is missing and no default provided
    """
    v = os.getenv(key, default)
    if v is None:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return v


def env_int(key: str, default: int) -> int:
    """
    Get an integer value from environment variables with fallback to default.
    
    Args:
        key: Environment variable name
        default: Default value if not found or invalid
        
    Returns:
        Integer value from environment or default
    """
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    """
    Get a float value from environment variables with fallback to default.
    
    Args:
        key: Environment variable name
        default: Default value if not found or invalid
        
    Returns:
        Float value from environment or default
    """
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def sanitize_path(value: str) -> str:
    """Sanitize a path-like string coming from env/cli.

    Removes inline shell-style comments (# ...) and trims whitespace. This
    prevents accidental inclusion of documentation fragments inside DATA_DIR
    or other path variables (e.g. "/opt/app/runstate  # state root").

    Args:
        value: Raw path string

    Returns:
        Sanitized path string
    """
    if not value:
        return value
    # Split on '#' and take the left-most segment (common inline comment style)
    cleaned = value.split('#', 1)[0].strip()
    # Collapse any internal excessive whitespace
    cleaned = ' '.join(cleaned.split())
    return cleaned


def ensure_dir(path: str) -> str:
    """Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path to create

    Returns:
        The same path that was passed in

    Note:
        Uses exist_ok=True to avoid errors if directory already exists.
    """
    if not path:
        return path
    # Defense in depth: sanitize here too, in case callers forgot.
    cleaned = sanitize_path(path)
    os.makedirs(cleaned, exist_ok=True)
    return cleaned


def setup_logger(log_dir: str, level: str = "INFO") -> logging.Logger:
    """
    Set up logging for the display client.
    
    Creates a logger that outputs to both console (stdout) and a log file.
    The logger is configured with timestamps and appropriate formatting.
    
    Args:
        log_dir: Directory where log files should be stored
        level: Logging level (DEBUG, INFO, WARN, ERROR)
        
    Returns:
        Configured logger instance
        
    Note:
        If the logger is already configured, returns the existing instance
        to avoid duplicate handlers.
    """
    log_dir = sanitize_path(log_dir)
    ensure_dir(log_dir)
    logger = logging.getLogger("display_client")
    if logger.handlers:
        return logger  # already configured
    
    # Map string levels to logging constants
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARN,
        "WARNING": logging.WARN,
        "ERROR": logging.ERROR,
    }
    logger.setLevel(level_map.get(level.upper(), logging.INFO))
    
    # Create formatter for consistent log format
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    
    # Console handler for real-time monitoring
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    
    # File handler for persistent logging; fall back gracefully on permission issues
    try:
        fh = logging.FileHandler(os.path.join(log_dir, "display_client.log"))
    except (OSError, PermissionError) as e:
        # We keep console logging only.
        logger.warning("File logging disabled (path=%s): %s", log_dir, e)
    else:
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    
    return logger


def sha256_bytes(data: bytes) -> str:
    """
    Calculate SHA256 hash of byte data.
    
    Args:
        data: Byte data to hash
        
    Returns:
        Hexadecimal string representation of the hash
    """
    return hashlib.sha256(data).hexdigest()


def parse_iso8601(ts: str) -> Optional[datetime]:
    """
    Parse ISO8601 timestamp string to datetime object.
    
    Handles both timezone-aware and timezone-naive timestamps,
    converting 'Z' suffix to proper UTC timezone.
    
    Args:
        ts: ISO8601 timestamp string
        
    Returns:
        Parsed datetime object or None if parsing fails
    """
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def combine_url(base: str, maybe_rel: str) -> str:
    """
    Combine base URL with a potentially relative URL.
    
    If maybe_rel is already absolute (has http/https scheme), returns it as-is.
    Otherwise, joins it with the base URL using proper URL joining rules.
    
    Args:
        base: Base URL
        maybe_rel: Potentially relative URL or path
        
    Returns:
        Complete absolute URL
    """
    # If maybe_rel is absolute, return as-is; else join with base
    if urlparse(maybe_rel).scheme in ("http", "https"):
        return maybe_rel
    return urljoin(base if base.endswith("/") else base + "/", maybe_rel.lstrip("/"))
