# Mimir Unified Display Client

Unified display client for Raspberry Pi supporting multiple hardware backends (Inky e‑ink, HyperPixel 4.0 Square, and simulation). Provides MQTT-based discovery + display pipeline with pluggable backends and orientation-aware rendering.

## Features
* Dynamic backend selection (`--backend` or `DISPLAY_BACKEND=auto`)
* Inky and HyperPixel RGB565 framebuffer support (multi-bpp, stride + RGB565 endianness/channel overrides)
* Orientation handling via `DISPLAY_ORIENTATION` (landscape / portrait_left / portrait_right)
* Startup logo image (centered) + optional HyperPixel test pattern (`STARTUP_TEST_PATTERN=1`)
* Simulation fallback when hardware missing
* Structured capabilities reporting (resolution, native resolution, orientation, rotation, formats)
* Systemd install script (interactive) + production installer + update script (`scripts/update_display.sh`)
* Hostname-based canonical device ID for stable MQTT topics

---

## Quick Install (Interactive)

```bash
curl -L https://raw.githubusercontent.com/<your-org>/mimir-display/main/scripts/install_display.sh -o install_display.sh
chmod +x install_display.sh
./install_display.sh
```

The script:
1. Prompts for backend (inky / hyperpixelsq / auto)
2. Prompts for display orientation (landscape / portrait_left / portrait_right)
3. Creates `.venv`
4. Installs with appropriate extras
5. Writes `.env` with `DISPLAY_BACKEND` + `DISPLAY_ORIENTATION`
6. Optionally installs a systemd service
7. (HyperPixel) Optionally appends overlay line to boot config
8. Displays a startup logo on service launch (customizable via `STARTUP_LOGO_PATH`)

## Manual Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .[all]   # or .[inky] / .[hyperpixelsq]
echo 'DISPLAY_BACKEND=auto' > .env
mimir-display --backend auto
```

## Environment Variables (.env)
| Key | Description | Example |
|-----|-------------|---------|
| DISPLAY_BACKEND | Backend selection (`auto`, `inky`, `hyperpixelsq`) | `DISPLAY_BACKEND=auto` |
| DISPLAY_ORIENTATION | Physical mounting: `landscape`, `portrait_left`, `portrait_right` | `DISPLAY_ORIENTATION=portrait_left` |
| STARTUP_LOGO_PATH | Override path to startup image (defaults to built-in `startup.png`) | `/opt/mimir-display/logo.png` |
| STARTUP_TEST_PATTERN | If `1` and HyperPixel backend, render diagnostic gradient before logo | `STARTUP_TEST_PATTERN=1` |
| LOG_LEVEL | Logging threshold | `INFO` |
| MQTT_BROKER_HOST / MQTT_BROKER_PORT | MQTT broker host/port | `oak.local` / `1883` |
| DISPLAY_ID | Optional override (hostname slug used otherwise) | `DISPLAY_ID=myframe01` |
| DEFAULT_CONTENT_PATH | Fallback image to show if assignments fail | `./example_default.png` |
| HYPERPIXEL_RGB565_ENDIAN | `little` or `big` override for RGB565 writes | `HYPERPIXEL_RGB565_ENDIAN=little` |
| HYPERPIXEL_RGB565_CHANNEL | `rgb` or `bgr` channel swap for RGB565 | `HYPERPIXEL_RGB565_CHANNEL=bgr` |
| HYPERPIXEL_FORCE_BPP | Force treat framebuffer as 16/24/32 bpp | `HYPERPIXEL_FORCE_BPP=16` |
| HYPERPIXEL_8888_SEQ | 4-byte sequence for 32bpp layout (chars in `RGBAX`) | `HYPERPIXEL_8888_SEQ=BGRX` |
| HYPERPIXEL_LOG_FIRST_BYTES | Log first N bytes of first framebuffer write | `HYPERPIXEL_LOG_FIRST_BYTES=64` |
| MIMIR_STATE_DIR / MIMIR_CACHE_DIR | Custom state + cache paths | `/var/lib/mimir-display/state` |
| WEBHOOK_ENABLED / WEBHOOK_PORT | Enable webhook server & port | `WEBHOOK_ENABLED=true` |
| STARTUP_LOGO_PATH | Custom logo path (repeated for emphasis) | `/opt/logo.png` |

> See `.env.example` for a more complete, commented template.

## Systemd Service Example
```ini
[Unit]
Description=Mimir Unified Display Client
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/mimir-display
EnvironmentFile=/opt/mimir-display/.env
ExecStart=/opt/mimir-display/.venv/bin/python -m mimir_display --backend auto
Restart=on-failure
RestartSec=3
User=pi
Group=video

[Install]
WantedBy=multi-user.target
```

Enable & start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mimir-display
```

## Backend Detection Logic
1. CLI `--backend`
2. `DISPLAY_BACKEND` env
3. Autodetect sequence:
	* Inspect `/dev/fb0` and its sysfs metadata (`/sys/class/graphics/fb0/virtual_size`, `bits_per_pixel`).
	* If size = `720x720` (and `bpp` = 16 when readable) → assume HyperPixel 4.0 Square (RGB565)
	* Else fallback to Inky
4. Simulation fallback (when backend import fails or forced)

Environment shortcuts:
* `FORCE_INKY=1` to bypass framebuffer detection
* `FORCE_SIM=1` to force simulation (primarily for development/testing)

## HyperPixel 4.0 Square Setup
The HyperPixel Square does not need a Python driver here; it appears as a Linux framebuffer (DPI) once its Device Tree overlay is enabled.

1. Edit your boot config (path varies by distro) adding:
	```
	dtoverlay=vc4-kms-dpi-hyperpixel4sq
	```
	Typical locations: `/boot/firmware/config.txt` (newer Raspberry Pi OS) or `/boot/config.txt`.
2. Reboot the Raspberry Pi; this should create `/dev/fb0` with a 720x720 16bpp framebuffer.
3. Run the installer and select `hyperpixelsq` (or choose `auto` and let autodetection pick it).

The installer script will offer to append the overlay line automatically if it is missing. After adding the overlay you must reboot before the framebuffer becomes available.

Troubleshooting:
* Run `cat /sys/class/graphics/fb0/virtual_size` → expect `720,720`.
* Run `cat /sys/class/graphics/fb0/bits_per_pixel` → expect `16`.
* If these differ, verify the overlay line and reboot again.

## Orientation Handling
Physical rotation is declared via `DISPLAY_ORIENTATION`. Portrait modes swap the logical reported resolution and apply an internal rotation so content is rendered upright while the hardware still receives native landscape-ordered buffers.

| Orientation | rotation_deg | Reported resolution (example native 720x720 square / 800x480 landscape) |
|-------------|--------------|-------------------------------------------------------------------------|
| landscape | 0 | 800x480 (unchanged) |
| portrait_left | 90 | 480x800 |
| portrait_right | 270 | 480x800 |

Square panels (e.g. HyperPixel 4.0 Square) treat landscape as the canonical native orientation; portrait rotates content but width/height remain equal.

## Startup Logo & Test Pattern
At startup the client will:
1. (Optional) Render a diagnostic gradient pattern when `STARTUP_TEST_PATTERN=1` and backend is HyperPixel.
2. Render a centered startup logo (`startup.png` or custom `STARTUP_LOGO_PATH`).

## Updating an Existing Installation
Use the provided update script to deploy code changes and restart the service:

```bash
scripts/update_display.sh                    # auto-detect install & restart
INSTALL_DIR=/opt/mimir-display scripts/update_display.sh
FORCE_SYNC=1 scripts/update_display.sh       # force rsync even if editable
PIP_EXTRAS='[hyperpixelsq]' scripts/update_display.sh
```

Key behaviors:
* Detects whether install is an editable in-place or a synced copy.
* Re-runs editable install with optional extras.
* Restarts systemd service if present.

## Adding a New Backend
Implement a module exposing: `get_display_capabilities()`, `display_image(path)`, `is_development_mode()`. Add it to the mapping in `hardware/loader.py`. Capabilities should include: `resolution`, `native_resolution`, `orientation`, `rotation_deg`, supported formats, backend name, and any diagnostic fields (e.g., stride, bpp).

## License
MIT
