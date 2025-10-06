"""Entry point for the mimir-display client (discovery mode only)."""

import sys
import logging
import signal
import contextlib
from pathlib import Path
import asyncio
import argparse
import platform
import os

from .mqtt_client_manager import run_mqtt_discovery_mode
from .hardware.loader import load_backend

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
    # Allow additional backends (rgbmatrix, hdmi). Use explicit list so --help stays informative.
    parser.add_argument(
        '--backend',
        choices=['inky', 'hyperpixelsq', 'rgbmatrix', 'hdmi', 'auto'],
        default='auto',
        help='Display backend selection (default: auto)'
    )
    return parser.parse_args()


def main():
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
        sys.exit(code)

    # Load backend (early so capabilities are available for downstream services)
    selected_backend = None
    try:
        explicit = None if args.backend == 'auto' else args.backend
        selected_backend = load_backend(explicit)
        caps = selected_backend.get_display_capabilities()
        logger.info("Backend selected=%s simulation=%s resolution=%s formats=%s", caps.get('backend', explicit or 'detected'), caps.get('simulation_mode'), caps.get('resolution'), caps.get('supported_formats'))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Backend load failed: %s", e)

    try:
        # If there's no running loop, this raises RuntimeError
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop running => safe to use asyncio.run()
        try:
            asyncio.run(runner())
        except KeyboardInterrupt:
            logger.info("Interrupted")
            sys.exit(130)
        else:
            sys.exit(0)
    else:
        # A loop is already running (e.g., inside uvicorn/Jupyter).
        # We cannot call asyncio.run(); instead schedule the task and return.
        logger.info("Event loop already running; scheduling runner() as a background task.")
        loop.create_task(runner())
        # Do NOT sys.exit() here; let the hosting process manage lifecycle.


if __name__ == "__main__":
    main()
