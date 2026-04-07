"""
Webhook server for manual display updates.

This module provides HTTP endpoints for external systems to trigger
manual updates and refreshes of display content.
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional


class WebhookHandler(BaseHTTPRequestHandler):
    """
    HTTP handler for manual update webhooks.
    
    This handler provides HTTP endpoints for external systems to trigger
    manual updates and refreshes of the display content. It's designed to
    work with deployment systems, monitoring tools, or manual triggers.
    
    Supported endpoints:
    - POST /update: Trigger immediate content check and update
    - POST /refresh: Force refresh of current content (bypass cache)
    - POST /config: Apply configuration payload (MQTT broker, platform URL)
    - GET /status: Get current display status information
    
    The handler requires a reference to the DisplayClient instance to
    access logging and trigger update flags.
    """
    
    def __init__(self, display_client, *args, **kwargs):
        """
        Initialize webhook handler with display client reference.
        
        Args:
            display_client: DisplayClient instance for accessing state and logging
        """
        self.display_client = display_client
        super().__init__(*args, **kwargs)
    
    def log_message(self, format, *args):
        """
        Override default logging to use display client logger.
        
        This ensures webhook logs are integrated with the main application
        logging system rather than going to stderr.
        """
        if hasattr(self, 'display_client') and self.display_client:
            self.display_client.logger.debug(f"Webhook: {format % args}")
    
    def do_POST(self):
        """
        Handle POST requests for manual updates and refreshes.
        
        Endpoints:
        - /update: Set force_update_flag to trigger immediate content check
        - /refresh: Set force_refresh_flag to bypass cache and refresh current content
        
        Returns JSON response with status confirmation.
        """
        try:
            path = self.path.strip('/')
            
            if path == 'update':
                # Trigger immediate content check
                self.display_client.logger.info("Manual update triggered via webhook")
                self.display_client.force_update_flag = True
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "update_triggered"}')
                
            elif path == 'refresh':
                # Force refresh current content
                self.display_client.logger.info("Manual refresh triggered via webhook")
                self.display_client.force_refresh_flag = True
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "refresh_triggered"}')
                
            elif path == 'config':
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0)
                payload = json.loads(raw.decode('utf-8')) if raw else {}
                self.display_client.logger.info("Config update received via webhook")
                self.display_client.apply_bootstrap_config(payload)
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"status": "config_applied"}')

            elif path == 'update-client':
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0)
                body = json.loads(raw.decode('utf-8')) if raw else {}
                branch = str(body.get('branch', 'main'))
                dry_run = bool(body.get('dry_run', False))

                from mimir_display.utils.update import trigger_update
                pid = trigger_update(
                    git_branch=branch,
                    dry_run=dry_run,
                    log=self.display_client.logger,
                )

                self.send_response(200 if pid else 503)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                resp = (
                    json.dumps({"status": "update_triggered", "pid": pid}).encode()
                    if pid
                    else b'{"status": "error", "detail": "update script not found"}'
                )
                self.wfile.write(resp)

            else:
                self.send_error(404, "Endpoint not found")
                
        except Exception as e:
            self.display_client.logger.error("Webhook error: %s", e)
            self.send_error(500, str(e))
    
    def do_GET(self):
        """
        Handle GET requests for status information.
        
        Endpoint:
        - /status: Return JSON with display ID, hostname, last update time,
                  and current assignment status
        
        Returns JSON response with current display status.
        """
        try:
            if self.path.strip('/') == 'status':
                status = {
                    "display_id": self.display_client.display_id,
                    "hostname": self.display_client.config.hostname,
                    "last_update": getattr(self.display_client, 'last_update_time', None),
                    "current_assignment": bool(getattr(self.display_client, 'current_assignment', None))
                }
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(status).encode())
            else:
                self.send_error(404, "Endpoint not found")
        except Exception as e:
            self.display_client.logger.error("Webhook status error: %s", e)
            self.send_error(500, str(e))


class WebhookServer:
    """
    Manages the webhook HTTP server for manual display updates.
    
    This class encapsulates the webhook server lifecycle, making it easier
    to start, stop, and manage the webhook service.
    """
    
    def __init__(self, display_client, port: int = 8081):
        """
        Initialize webhook server.
        
        Args:
            display_client: DisplayClient instance
            port: Port to listen on
        """
        self.display_client = display_client
        self.port = port
        self.server: Optional[HTTPServer] = None
        self.thread: Optional[threading.Thread] = None
        self.logger = display_client.logger
    
    def start(self):
        """Start the webhook server."""
        if not self.display_client.config.webhook_enabled:
            self.logger.debug("Webhook server disabled")
            return
        
        try:
            self.logger.debug("Creating webhook server on port %d", self.port)
            
            # Create a factory function that passes the display client to the handler
            def handler_factory(*args, **kwargs):
                return WebhookHandler(self.display_client, *args, **kwargs)

            self.server = HTTPServer(('0.0.0.0', self.port), handler_factory)
            
            # Start server in a separate thread
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
                name="WebhookServer"
            )
            self.thread.start()
            
            self.logger.info("Webhook server started on port %d", self.port)
            self.logger.info("Manual update endpoints:")
            self.logger.info("  POST /update         - Trigger immediate content check")
            self.logger.info("  POST /refresh        - Force refresh current content")
            self.logger.info("  POST /update-client  - git pull + reinstall + restart")
            self.logger.info("  GET  /status         - Get display status")
            
        except Exception as e:
            self.logger.warning("Failed to start webhook server: %s", e)
    
    def is_running(self) -> bool:
        """Return True if the server thread is alive."""
        return self.thread is not None and self.thread.is_alive()

    def stop(self):
        """Stop the webhook server."""
        try:
            if self.server:
                self.server.shutdown()
                self.server.server_close()
                self.logger.info("Webhook server stopped")
                
            if self.thread:
                self.thread.join(timeout=5)
                
        except Exception as e:
            self.logger.warning("Error stopping webhook server: %s", e)
        finally:
            self.server = None
            self.thread = None
