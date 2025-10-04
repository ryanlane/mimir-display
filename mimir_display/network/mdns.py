"""
mDNS service management for device discovery.

This module handles mDNS service broadcasting to make the display
discoverable on the local network for automatic platform integration.
"""

# mimir_display/network/mdns.py
import socket, ipaddress
from datetime import datetime, timezone
from typing import Optional
from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

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
            props[b"last_seen"] = datetime.now(timezone.utc).isoformat().encode()
            updated = ServiceInfo(
                self.service.type, self.service.name,
                addresses=self.service.addresses, port=self.service.port,
                properties=props, server=self.service.server,
            )
            await self.azc.async_update_service(updated)  # <-- async API
            self.service = updated
        except Exception as e:
            self.logger.debug("Failed to update mDNS properties: %s", e)

