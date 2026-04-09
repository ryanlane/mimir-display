"""
Provisioning HTTP server.

Serves a simple web UI for bootstrapping the display client when it has no
server configuration. The user scans the QR code on the display, opens the
page in a browser, pastes a provision bundle copied from the Mimir web UI,
and submits. The server calls on_provisioned() which writes device_config.json;
the process then exits so systemd restarts it with the new config applied.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 7777

# ── HTML (self-contained, no external deps) ────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mimir Display Setup</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     background:#0f0f1a;color:#e0e0ff;min-height:100vh;display:flex;
     align-items:center;justify-content:center;padding:1rem}}
.card{{background:#1a1a2e;border:1px solid #2a2a4a;border-radius:12px;
      padding:2rem;max-width:480px;width:100%}}
h1{{font-size:1.4rem;font-weight:700;margin-bottom:.25rem;color:#fff}}
.sub{{font-size:.875rem;color:#888;margin-bottom:1.5rem;line-height:1.5}}
.meta{{background:#12122a;border:1px solid #2a2a4a;border-radius:8px;
      padding:.75rem 1rem;margin-bottom:1.5rem;font-size:.8rem;color:#aaa;
      display:flex;flex-direction:column;gap:.3rem}}
.meta b{{color:#c0b8ff}}
label{{display:block;font-size:.875rem;font-weight:600;color:#aaa;margin-bottom:.4rem}}
textarea{{width:100%;height:110px;background:#12122a;border:1px solid #3a3a5a;
         border-radius:8px;padding:.75rem;color:#e0e0ff;font-size:.8rem;
         font-family:monospace;resize:vertical;outline:none}}
textarea:focus{{border-color:#7c6af7}}
.hint{{font-size:.75rem;color:#666;margin:.4rem 0 1rem;line-height:1.5}}
.hint strong{{color:#888}}
button{{width:100%;padding:.8rem;background:#7c6af7;color:#fff;border:none;
       border-radius:8px;font-size:1rem;font-weight:600;cursor:pointer;transition:opacity .15s}}
button:hover{{opacity:.85}}
button:disabled{{opacity:.4;cursor:not-allowed}}
.msg{{margin-top:1rem;padding:.75rem 1rem;border-radius:8px;font-size:.875rem;display:none;line-height:1.5}}
.msg.ok{{background:rgba(76,175,80,.15);border:1px solid rgba(76,175,80,.3);color:#81c784}}
.msg.err{{background:rgba(229,57,53,.12);border:1px solid rgba(229,57,53,.3);color:#ef9a9a}}
</style>
</head>
<body>
<div class="card">
  <h1>Mimir Display Setup</h1>
  <p class="sub">Paste a provision bundle from the Mimir server to connect this display.</p>
  <div class="meta">
    <div><b>Hostname:</b> {hostname}</div>
    <div><b>IP Address:</b> {ip}</div>
  </div>
  <label for="bundle">Provision Bundle</label>
  <textarea id="bundle" placeholder="Paste bundle here\u2026" spellcheck="false"></textarea>
  <p class="hint">In the Mimir web UI go to <strong>Displays &rarr; Get Provision Bundle</strong>, copy the string, and paste it above.</p>
  <button id="btn" onclick="applyBundle()">Apply Configuration</button>
  <div id="msg" class="msg"></div>
</div>
<script>
async function applyBundle() {{
  const bundle = document.getElementById('bundle').value.trim();
  const btn = document.getElementById('btn');
  const msg = document.getElementById('msg');
  msg.style.display = 'none';
  if (!bundle) {{ showMsg('Paste a provision bundle first.', false); return; }}
  btn.disabled = true;
  btn.textContent = 'Applying\u2026';
  try {{
    const r = await fetch('/provision', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{bundle}})
    }});
    const data = await r.json();
    if (r.ok) {{
      showMsg('\u2713 Configured! The display service is restarting\u2026', true);
      btn.textContent = 'Done';
    }} else {{
      showMsg('Error: ' + (data.detail || r.status), false);
      btn.disabled = false;
      btn.textContent = 'Apply Configuration';
    }}
  }} catch(e) {{
    showMsg('Network error: ' + e.message, false);
    btn.disabled = false;
    btn.textContent = 'Apply Configuration';
  }}
}}
function showMsg(text, ok) {{
  const el = document.getElementById('msg');
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.style.display = 'block';
}}
</script>
</body>
</html>
"""


# ── Bundle codec (shared with the server-side API) ────────────────────────────

BUNDLE_VERSION = 1


def decode_bundle(bundle_str: str) -> dict:
    """Decode and validate a base64 provision bundle string.

    Raises ValueError on malformed input.
    """
    try:
        raw = base64.b64decode(bundle_str.strip().encode()).decode("utf-8")
        data = json.loads(raw)
    except Exception as exc:
        raise ValueError(f"Invalid bundle (cannot decode): {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Bundle must be a JSON object")
    if data.get("v") != BUNDLE_VERSION:
        raise ValueError(f"Unsupported bundle version: {data.get('v')!r}")
    if not data.get("platform_url"):
        raise ValueError("Bundle missing platform_url")
    return data


def encode_bundle(
    platform_url: str,
    mqtt_host: str,
    mqtt_port: int = 1883,
    mqtt_username: Optional[str] = None,
    mqtt_password: Optional[str] = None,
) -> str:
    """Encode connection details into a base64 provision bundle string."""
    payload = {
        "v": BUNDLE_VERSION,
        "platform_url": platform_url,
        "mqtt_host": mqtt_host,
        "mqtt_port": mqtt_port,
        "mqtt_username": mqtt_username,
        "mqtt_password": mqtt_password,
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


# ── HTTP server ────────────────────────────────────────────────────────────────

def start_provisioning_server(
    hostname: str,
    ip_address: str,
    on_provisioned: Callable[[dict], None],
    port: int = _DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Start the provisioning server in a daemon thread.

    Args:
        hostname:       Device hostname shown in the web UI.
        ip_address:     Device LAN IP shown in the web UI.
        on_provisioned: Callback invoked with the decoded bundle dict when the
                        user successfully submits a bundle.
        port:           TCP port to listen on (default 7777).

    Returns the running ThreadingHTTPServer instance.
    """
    html = _HTML_TEMPLATE.format(hostname=hostname, ip=ip_address)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/setup"):
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/provision":
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                config = decode_bundle(body.get("bundle", ""))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"detail": str(exc)})
                return

            # Validate the bundle can be applied before sending success.
            # The actual apply happens in a background thread after the response
            # is flushed, so sys.exit() doesn't kill the socket mid-write.
            self._send_json(200, {"status": "ok"})

            def _apply() -> None:
                import time
                time.sleep(0.5)
                try:
                    on_provisioned(config)
                except Exception as exc:  # pragma: no cover
                    logger.error("Provisioning callback failed: %s", exc)

            threading.Thread(target=_apply, daemon=True).start()

        def _send_json(self, code: int, data: dict) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: object) -> None:  # silence default
            logger.debug("provision-http: " + fmt, *args)

    try:
        server = ThreadingHTTPServer(("", port), _Handler)
    except OSError as exc:
        logger.error("Failed to bind provisioning server on port %d: %s", port, exc)
        raise

    thread = threading.Thread(target=server.serve_forever, name="mimir.provision", daemon=True)
    thread.start()
    logger.info(
        "Provisioning server listening — open http://%s:%d/setup to configure this display",
        ip_address, port,
    )
    return server
