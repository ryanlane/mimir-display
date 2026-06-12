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

from __future__ import annotations

import hashlib
import logging
import os
import socket
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse, urlunparse


def env_str(key: str, default: str | None = None) -> str:
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
    # Basic validation: reject obviously bad accidental values like lone parentheses or commas
    if len(cleaned) <= 2 and cleaned in {"(", ")", "{}", "[]", "<>", "''", '""', ","}:
        raise ValueError(f"Suspicious directory path '{cleaned}' (sanitized from '{path}')")
    try:
        os.makedirs(cleaned, exist_ok=True)
        return cleaned
    except PermissionError:
        # If it's a relative path (starts with '.' or no leading slash) and we lack permission,
        # attempt a fallback under /var/lib/mimir-display (typical writable hierarchy when running as root).
        if not os.path.isabs(cleaned):
            fallback_root = "/var/lib/mimir-display"
            fallback = os.path.join(fallback_root, cleaned.lstrip('./'))
            try:
                os.makedirs(fallback, exist_ok=True)
            except Exception:
                # Re-raise original if fallback also fails
                raise
            else:
                logging.getLogger("display_client").warning(
                    "Directory '%s' not writable; using fallback '%s'", cleaned, fallback
                )
                return fallback
        raise


def resolve_writable_dir(preferred: str | None, purpose: str, subdir: str | None = None) -> str:
    """Choose and create a writable directory for a given purpose.

    Resolution chain (first that can be created/written wins):
        1. Explicit preferred path (DATA_DIR or caller provided)
        2. $XDG_DATA_HOME/mimir-display
        3. ~/.local/share/mimir-display
        4. /var/lib/mimir-display (often pre-created for system installs)
        5. /tmp/mimir-display

    Args:
        preferred: User supplied base directory (may be None/empty)
        purpose: Short label used only for warning messages
        subdir: Optional child directory to append (e.g. "logs", "cache")

    Returns:
        Absolute path to a writable directory (created if needed)

    Raises:
        RuntimeError if no candidate path could be created.
    """
    candidates: list[str] = []
    if preferred:
        try:
            cand = sanitize_path(preferred)
            if cand:
                candidates.append(cand)
        except Exception:
            pass
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        candidates.append(os.path.join(xdg, "mimir-display"))
    home = os.path.expanduser("~")
    if home and home != "~":  # ensure expansion worked
        candidates.append(os.path.join(home, ".local", "share", "mimir-display"))
    # System-level typical location (might not be writable as non-root, that's fine)
    candidates.append("/var/lib/mimir-display")
    # Last resort
    candidates.append("/tmp/mimir-display")

    tried: list[str] = []
    for base in candidates:
        target = os.path.join(base, subdir) if subdir else base
        try:
            ensured = ensure_dir(target)
            return ensured
        except Exception as e:  # pragma: no cover - best effort path selection
            tried.append(f"{target} -> {type(e).__name__}")
            continue
    raise RuntimeError(f"Unable to establish writable directory for {purpose}; attempted: {tried}")


class _ColorFormatter(logging.Formatter):
    """ANSI-color formatter for TTY output (direct terminal or SSH)."""

    _RESET = "\033[0m"
    _LEVEL_STYLES = {
        logging.DEBUG:    "\033[2m",        # dim
        logging.INFO:     "\033[36m",       # cyan
        logging.WARNING:  "\033[33m",       # yellow
        logging.ERROR:    "\033[31m",       # red
        logging.CRITICAL: "\033[1;31m",     # bold red
    }
    _DIM = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._LEVEL_STYLES.get(record.levelno, "")
        timestamp = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return (
            f"{self._DIM}{timestamp}{self._RESET} "
            f"{color}[{record.levelname}]{self._RESET} "
            f"{msg}"
        )


class _JournaldFormatter(logging.Formatter):
    """Prepends syslog priority prefix so journald records the correct level.

    journalctl then colours by priority automatically:
      CRITICAL/ERROR → red, WARNING → yellow, INFO → normal, DEBUG → dim
    """

    _SD_PREFIX = {
        logging.CRITICAL: "<2>",
        logging.ERROR:    "<3>",
        logging.WARNING:  "<4>",
        logging.INFO:     "<6>",
        logging.DEBUG:    "<7>",
    }

    def format(self, record: logging.LogRecord) -> str:
        prefix = self._SD_PREFIX.get(record.levelno, "<6>")
        return prefix + super().format(record)


def setup_logger(log_dir: str, level: str = "INFO") -> logging.Logger:
    """Set up logging for the display client.

    - TTY (direct terminal or SSH): ANSI-colored output.
    - Non-TTY (systemd/journald pipe): syslog priority prefixes so
      ``journalctl`` colours by level automatically.
    - File handler always writes plain text.
    """
    log_dir = sanitize_path(log_dir)
    ensure_dir(log_dir)
    logger = logging.getLogger("display_client")
    if logger.handlers:
        return logger  # already configured

    level_map = {
        "DEBUG":   logging.DEBUG,
        "INFO":    logging.INFO,
        "WARN":    logging.WARNING,
        "WARNING": logging.WARNING,
        "ERROR":   logging.ERROR,
    }
    logger.setLevel(level_map.get(level.upper(), logging.INFO))

    plain_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Console handler — pick formatter based on whether stdout is a TTY
    ch = logging.StreamHandler(sys.stdout)
    if sys.stdout.isatty():
        ch.setFormatter(_ColorFormatter())
    else:
        ch.setFormatter(_JournaldFormatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    # File handler — always plain text, no escape codes
    try:
        fh = logging.FileHandler(os.path.join(log_dir, "display_client.log"))
    except (OSError, PermissionError) as e:
        logger.warning("File logging disabled (path=%s): %s", log_dir, e)
    else:
        fh.setFormatter(plain_fmt)
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


def parse_iso8601(ts: str) -> datetime | None:
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


def resolve_dot_local_url(url: str) -> tuple[str, str | None]:
    """Resolve an mDNS .local hostname in a URL to its IP address.

    Many environments (Docker, some Linux network stacks) cannot resolve
    .local hostnames via the normal resolver that aiohttp uses, but
    ``socket.gethostbyname`` works because it goes through the system's
    mDNS stack (avahi / systemd-resolved).

    Returns:
        (rewritten_url, host_header) where:
          - rewritten_url has the hostname replaced with the resolved IP
            (unchanged if the host is not .local or resolution fails)
          - host_header is the original .local hostname to send as the
            HTTP ``Host:`` header (None if no rewriting was done)
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        if not hostname.endswith(".local"):
            return url, None
        resolved_ip = socket.gethostbyname(hostname)
        port_suffix = f":{parsed.port}" if parsed.port else ""
        rebuilt = parsed._replace(netloc=resolved_ip + port_suffix)
        return urlunparse(rebuilt), hostname
    except Exception:
        return url, None


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
