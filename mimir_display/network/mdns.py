"""
mDNS service management for device discovery.

This module handles:
  - mDNS service broadcasting so the server can discover displays
  - mDNS server discovery so displays can find the Mimir server
    without needing PLATFORM_URL set in .env
"""

# mimir_display/network/mdns.py
import asyncio
import ipaddress
import socket
from datetime import datetime, timezone
from typing import Optional

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

MIMIR_SERVER_SERVICE_TYPE = "_mimir._tcp.local."


def _sync_discover_mimir_server(timeout: float) -> Optional[str]:
    """Blocking scan for _mimir._tcp.local. — run this in a thread."""
    import threading

    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    result: list[Optional[str]] = [None]
    found = threading.Event()

    class _Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
            info = zc.get_service_info(service_type, name, timeout=1000)
            if not info or not info.addresses:
                return
            addrs = [
                socket.inet_ntoa(a)
                for a in info.addresses
                if len(a) == 4 and not socket.inet_ntoa(a).startswith("127.")
            ]
            if addrs:
                result[0] = f"http://{addrs[0]}:{info.port}"
                found.set()

        def update_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
            self.add_service(zc, service_type, name)

        def remove_service(self, zc: Zeroconf, service_type: str, name: str) -> None:
            pass

    zc = Zeroconf()
    browser = ServiceBrowser(zc, MIMIR_SERVER_SERVICE_TYPE, _Listener())
    found.wait(timeout=timeout)
    try:
        browser.cancel()
    except Exception:
        pass
    try:
        zc.close()
    except Exception:
        pass
    return result[0]


async def discover_mimir_server(timeout: float = 5.0) -> Optional[str]:
    """Browse mDNS for a Mimir server advertisement.

    Returns the server base URL (e.g. 'http://192.168.1.50:5000') if found
    within *timeout* seconds, or None if no server is found.

    The server must be advertising _mimir._tcp.local. — this is done
    automatically by the mimir-discovery sidecar when it runs.
    """
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _sync_discover_mimir_server, timeout),
            timeout=timeout + 2,
        )
    except (asyncio.TimeoutError, Exception):
        return None

class MDNSService:
    def __init__(self, display_client):
        self.display_client = display_client
        self.config = display_client.config
        self.logger = display_client.logger
        self.service: Optional[ServiceInfo] = None
        self.azc: Optional[AsyncZeroconf] = None

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    def is_running(self) -> bool:
        return self.azc is not None and self.service is not None

    async def start(self) -> bool:
        if not self.display_client.display_id:
            self.logger.info("mDNS not started: no display_id yet")
            return False
        if self.is_running():
            await self.update_properties()
            self.logger.info("mDNS already running; properties updated")
            return True
        try:
            local_ip = self._get_local_ip()
            if local_ip.startswith("127."):
                self.logger.info("mDNS binding to 127.0.0.1; discovery may be limited")

            name = f"mimir-display-{self.display_client.display_id}._mimir-display._tcp.local."
            caps = getattr(self.display_client, 'capabilities', {}) or {}
            orientation = caps.get('orientation') or 'landscape'
            rotation = caps.get('rotation_deg')
            native_res = caps.get('native_resolution')
            native_res_str = None
            if isinstance(native_res, (list, tuple)) and len(native_res) == 2:
                native_res_str = f"{native_res[0]}x{native_res[1]}"

            info = ServiceInfo(
                type_="_mimir-display._tcp.local.",
                name=name,
                addresses=[ipaddress.IPv4Address(local_ip).packed],
                port=self.config.webhook_port if self.config.webhook_enabled else 8080,
                properties={
                    b"display_id": self.display_client.display_id.encode(),
                    b"display_name": self.config.display_name.encode(),
                    b"location": self.config.display_location.encode(),
                    b"hostname": self.config.hostname.encode(),
                    b"webhook_port": (str(self.config.webhook_port).encode()
                                     if self.config.webhook_enabled else b""),
                    b"resolution": f"{self.display_client.capabilities['resolution'][0]}x{self.display_client.capabilities['resolution'][1]}".encode(),
                    b"platform_url": self.config.platform_url.encode(),
                    b"client_version": self.config.get("client_version", "1.0.0").encode(),
                    b"last_seen": datetime.now(timezone.utc).isoformat().encode(),
                    b"orientation": orientation.encode(),
                    b"rotation_deg": (str(rotation).encode() if rotation is not None else b""),
                    b"native_resolution": (native_res_str.encode() if native_res_str else b""),
                    b"supports_animation": (b"1" if caps.get("supports_animation") else b"0"),
                },
                server=f"mimir-display-{self.display_client.display_id}.local.",
            )

            self.azc = AsyncZeroconf()
            await self.azc.async_register_service(info)  # <-- async API
            self.service = info
            self.logger.info("mDNS service started: %s at %s", name, local_ip)
            return True

        except Exception as e:
            self.logger.exception("Failed to start mDNS service (%r)", e)
            return False

    async def stop(self):
        try:
            if self.azc and self.service:
                try:
                    await self.azc.async_unregister_service(self.service)
                finally:
                    await self.azc.async_close()
                self.logger.info("mDNS service stopped")
        except Exception as e:
            self.logger.warning("Error stopping mDNS service: %s", e)
        finally:
            self.azc = None
            self.service = None

    async def update_properties(self):
        if not (self.azc and self.service):
            return
        try:
            props = dict(self.service.properties)
            caps = getattr(self.display_client, 'capabilities', {}) or {}
            props[b"last_seen"] = datetime.now(timezone.utc).isoformat().encode()
            # Update dynamic orientation / rotation in case env changed at runtime
            orientation = caps.get('orientation') or 'landscape'
            rotation = caps.get('rotation_deg')
            native_res = caps.get('native_resolution')
            native_res_str = None
            if isinstance(native_res, (list, tuple)) and len(native_res) == 2:
                native_res_str = f"{native_res[0]}x{native_res[1]}"
            props[b"orientation"] = orientation.encode()
            props[b"rotation_deg"] = (str(rotation).encode() if rotation is not None else b"0")
            if native_res_str:
                props[b"native_resolution"] = native_res_str.encode()
            updated = ServiceInfo(
                self.service.type, self.service.name,
                addresses=self.service.addresses, port=self.service.port,
                properties=props, server=self.service.server,
            )
            await self.azc.async_update_service(updated)  # <-- async API
            self.service = updated
        except Exception as e:
            self.logger.debug("Failed to update mDNS properties: %s", e)

