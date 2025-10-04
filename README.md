# Mimir Unified Display Client

Unified display client for Raspberry Pi supporting multiple hardware backends (Inky e‑ink, HyperPixel 4.0 Square, and simulation). Provides MQTT-based discovery + display pipeline with pluggable backends.

## Features
* Dynamic backend selection (`--backend` or `DISPLAY_BACKEND=auto`)
* Inky and HyperPixel RGB565 framebuffer support
* Simulation fallback when hardware missing
* Structured capabilities reporting (resolution, orientation, formats)
* Optional systemd service installer

## Quick Install (Interactive)

```bash
curl -L https://raw.githubusercontent.com/<your-org>/mimir-display/main/scripts/install_display.sh -o install_display.sh
chmod +x install_display.sh
./install_display.sh
```

The script:
1. Prompts for backend (inky / hyperpixelsq / auto)
2. Creates `.venv`
3. Installs with appropriate extras
4. Writes `.env` with `DISPLAY_BACKEND`
5. Optionally installs a systemd service

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
| DISPLAY_BACKEND | inky | hyperpixelsq | auto | DISPLAY_BACKEND=auto |
| LOG_LEVEL | Logging threshold | INFO |
| MQTT_BROKER | (future) override broker host | oak.local |

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
* `FORCE_SIM=1` to force simulation (primarily for development)

## HyperPixel 4.0 Square Setup
The HyperPixel Square does not need a Python driver here; it appears as a Linux framebuffer (DPI) once its Device Tree overlay is enabled.

1. Edit your boot config (path varies by distro) adding:
	```
	dtoverlay=vc4-kms-dpi-hyperpixel4sq
	```
	Typical locations: `/boot/firmware/config.txt` (newer Raspberry Pi OS) or `/boot/config.txt`.
2. Reboot the Raspberry Pi; this should create `/dev/fb0` with a 720x720 16bpp framebuffer.
3. Run the installer and select `hyperpixelsq` (or choose `auto` and let autodetection pick it).

The installer script will offer to append the overlay line automatically if it is missing.

Troubleshooting:
* Run `cat /sys/class/graphics/fb0/virtual_size` → expect `720,720`.
* Run `cat /sys/class/graphics/fb0/bits_per_pixel` → expect `16`.
* If these differ, verify the overlay line and reboot again.

## Adding a New Backend
Implement module with functions: `get_display_capabilities()`, `display_image(path)`, `is_development_mode()`. Add to `hardware/loader.py` map. Provide resolution + formats.

## License
MIT
