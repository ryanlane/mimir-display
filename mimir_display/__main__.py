"""Entry point for the mimir-display client (discovery mode only)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import platform
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .hardware.loader import load_backend
from .mqtt_client_manager import run_mqtt_discovery_mode

# Add the parent directory to sys.path for development
project_root = Path(__file__).parent.parent
if project_root not in sys.path:
    sys.path.insert(0, str(project_root))


def load_environment():
    """Load environment variables from .env file."""
    try:
        from dotenv import load_dotenv  # type: ignore
        env_files = ['.env', '.env.local', 'runstate/.env']
        for env_file in env_files:
            env_path = project_root / env_file
            if env_path.exists():
                load_dotenv(env_path)
                break
    except ImportError:
        # .env support is optional
        pass


def setup_logging(log_level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def diagnose_environment() -> int:
    """Print diagnostic information for deployment and exit.

    Surfaces runtime details useful for troubleshooting, especially on legacy
    Raspberry Pi Zero (armv6) installs. Safe to run as a non-root user; reads
    environment only and performs lightweight filesystem checks.
    """
    print("=== mimir-display environment diagnostics ===")
    print(f"Python: {platform.python_version()} ({platform.python_implementation()})")
    print(f"Platform: {platform.platform()}")
    print(f"Machine: {platform.machine()}")
    # Optional libs
    try:  # pragma: no cover - defensive
        import numpy  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover
        print("numpy: not importable (ModuleNotFoundError)")
    else:  # pragma: no cover - numpy imported successfully
        try:
            print(f"numpy: {numpy.__version__}")
        except AttributeError:
            print("numpy: loaded but __version__ missing")
    legacy_marker = Path('/var/lib/mimir-display/LEGACY_ZERO')
    print(f"Legacy marker present: {legacy_marker.exists()}")
    state_dir = os.environ.get('MIMIR_STATE_DIR') or '/var/lib/mimir-display/state'
    cache_dir = os.environ.get('MIMIR_CACHE_DIR') or '/var/lib/mimir-display/cache'
    print(f"State dir: {state_dir} (exists={Path(state_dir).exists()})")
    print(f"Cache dir: {cache_dir} (exists={Path(cache_dir).exists()})")
    for label, p in [('state', state_dir), ('cache', cache_dir)]:
        path_obj = Path(p)
        writable = path_obj.exists() and os.access(str(path_obj), os.W_OK)
        print(f"  {label} writable: {writable}")
    print(f"Effective UID: {os.geteuid() if hasattr(os, 'geteuid') else 'n/a'}")
    print("=============================================")
    return 0


async def runner():
    logger = logging.getLogger("display_client")
    shutdown_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            # Windows/Jupyter fallback: rely on KeyboardInterrupt
            pass

    task = asyncio.create_task(run_mqtt_discovery_mode(), name="mimir.discovery")

    try:
        # Wait for shutdown signal
        await shutdown_event.wait()
        logger.info("Shutdown signal received; cancelling discovery...")
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received; cancelling discovery...")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def parse_args():
    parser = argparse.ArgumentParser(description="mimir-display entrypoint")
    parser.add_argument('--diagnose-env', action='store_true', help='Print environment diagnostics and exit')
    parser.add_argument('--health', action='store_true', help='Print JSON health summary and exit (exit codes: 0 ok, 1 degraded, 2 error)')
    parser.add_argument('--health-server', action='store_true', help='Start lightweight HTTP health server (serves /health)')
    parser.add_argument('--health-port', type=int, default=8081, help='Port for --health-server (default: 8081)')
    # Allow additional backends (rgbmatrix, hdmi). Use explicit list so --help stays informative.
    parser.add_argument(
        '--backend',
        choices=['inky', 'hyperpixelsq', 'rgbmatrix', 'hdmi', 'auto'],
        default='auto',
        help='Display backend selection (default: auto)'
    )
    return parser.parse_args()


def _health_status(caps: dict | None) -> tuple[dict, int]:
    """Derive health JSON and exit code.

    Returns:
        (payload, exit_code)

    exit_code semantics:
        0 -> ok (hardware present, no init_error, not simulation)
        1 -> degraded (simulation mode OR missing backend info)
        2 -> error (init_error present)
    """
    if caps is None:
        payload = {"status": "error", "reason": "backend_load_failed"}
        return payload, 2
    init_error = caps.get('init_error')
    simulation = caps.get('simulation_mode')
    status = 'ok'
    exit_code = 0
    reason: list[str] = []
    if init_error:
        status = 'error'
        exit_code = 2
        reason.append(f"init_error:{init_error}")
    elif simulation:
        status = 'degraded'
        exit_code = 1
        reason.append('simulation_mode')
    payload = {
        'status': status,
        'backend': caps.get('backend'),
        'resolution': caps.get('resolution'),
        'native_resolution': caps.get('native_resolution'),
        'simulation_mode': simulation,
        'init_error': init_error,
        'reasons': reason,
    }
    return payload, exit_code


def _start_health_http_server(caps_provider, port: int, logger: logging.Logger) -> None:
    """Start a background HTTP server exposing /health.

    Args:
        caps_provider: Callable returning current capabilities dict.
        port: Port number to bind.
        logger: Logger for informational messages.
    """
    class Handler(BaseHTTPRequestHandler):  # type: ignore[misc]
        def do_GET(self):  # noqa: N802 - framework method name
            if self.path.split('?')[0] == '/health':
                try:
                    caps = caps_provider()
                except Exception as e:  # pragma: no cover - defensive
                    caps = None  # type: ignore
                    logger.warning("Health caps provider failed: %s", e)
                payload, _ = _health_status(caps)
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):  # noqa: D401 - silence default logging
            return

    try:
        server = ThreadingHTTPServer(('', port), Handler)
    except OSError as e:  # pragma: no cover - bind error
        logger.error("Failed to bind health server on port %s: %s", port, e)
        return

    thread = threading.Thread(target=server.serve_forever, name='mimir.health', daemon=True)
    thread.start()
    logger.info("Health server listening on port %s (GET /health)", port)


def main() -> int:
    """
    Synchronous entrypoint that works in both contexts:
    - If no event loop is running: uses asyncio.run(runner()).
    - If a loop is already running (embedded): schedules runner() on that loop.
    """
    args = parse_args()
    load_environment()
    setup_logging()
    logger = logging.getLogger("display_client")

    if getattr(args, 'diagnose_env', False):
        code = diagnose_environment()
        return code

    # Load backend (early so capabilities are available for downstream services)
    selected_backend = None
    try:
        explicit = None if args.backend == 'auto' else args.backend
        selected_backend = load_backend(explicit)
        caps = selected_backend.get_display_capabilities()
        logger.info(
            "Backend selected=%s simulation=%s resolution=%s formats=%s",
            caps.get('backend', explicit or 'detected'),
            caps.get('simulation_mode'), caps.get('resolution'), caps.get('supported_formats')
        )
    except Exception as e:  # pragma: no cover - defensive
        caps = None  # type: ignore
        logger.warning("Backend load failed: %s", e)

    # Early health check action
    if getattr(args, 'health', False):
        payload, exit_code = _health_status(caps)
        print(json.dumps(payload, indent=2))
        return exit_code

    # Optional health server
    if getattr(args, 'health_server', False):
        _start_health_http_server(lambda: selected_backend.get_display_capabilities() if selected_backend else None, args.health_port, logger)

    # Detect whether a loop is already running without leaving an active exception
    # in scope. Calling asyncio.run() from inside an `except` block causes Python
    # to attach the caught RuntimeError as __context__ on every exception that
    # surfaces from the async code — producing confusing chained tracebacks.
    _running_loop = None
    try:
        _running_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass

    if _running_loop is not None:
        # A loop is already running (e.g., inside uvicorn/Jupyter).
        # We cannot call asyncio.run(); instead schedule the task and return.
        logger.info("Event loop already running; scheduling runner() as a background task.")
        _running_loop.create_task(runner())
        # Do NOT sys.exit() here; let the hosting process manage lifecycle.
        return 0

    # No loop running — safe to use asyncio.run(), called outside any except
    # block so its exceptions carry no inherited __context__.
    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130
    except RuntimeError as e:  # pragma: no cover - defensive
        # On some Python versions a late signal during startup can surface
        # as a 'no running event loop' error while shutting down. Treat as clean exit.
        if 'no running event loop' in str(e).lower():
            logger.debug("Suppressed benign shutdown RuntimeError: %s", e)
            return 0
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
