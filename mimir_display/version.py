"""Client version information.

Standalone module (no package-level imports) so it can be imported from
anywhere — including ``mimir_display/__init__.py`` and the MQTT client —
without circular-import risk.

``CLIENT_VERSION`` comes from the installed package metadata, which is the
same value as ``project.version`` in ``pyproject.toml`` and therefore matches
the release tag / OTA artifact version.

``PROTOCOL_VERSION`` is the MQTT/HTTP contract version between display
clients and the Mimir server. Bump it (and the server's minimum) only when
the contract changes incompatibly.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

PROTOCOL_VERSION = 1

try:
    CLIENT_VERSION: str = version("mimir-display")
except PackageNotFoundError:  # running from a source tree without install
    CLIENT_VERSION = "0.0.0+dev"
