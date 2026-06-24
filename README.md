# Mimir Unified Display Client

Unified display client for Raspberry Pi supporting multiple hardware backends (Inky e‑ink, HyperPixel 4.0 Square, and simulation). Provides MQTT-based discovery + display pipeline with pluggable backends and orientation-aware rendering.

## Features
* Dynamic backend selection (`--backend` or `DISPLAY_BACKEND=auto`)
* Inky and HyperPixel RGB565 framebuffer support (multi-bpp, stride + RGB565 endianness/channel overrides)
* Generic HDMI fullscreen window backend (pygame or tkinter fallback)
* RGB LED Matrix (hzeller/rpi-rgb-led-matrix) backend for HUB75 panels (via `rgbmatrix` bindings)
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

## Task Runner

All common operations are available via [Task](https://taskfile.dev). Install it once:

```bash
sh -c "$(curl --location https://taskfile.dev/install.sh)" -- -d -b /usr/local/bin
```

Run `task` in this directory to list all commands. Most-used:

```bash
# Local development
task install                  # create .venv and install dependencies
task install:pi               # on-device install with inky + qrcode extras
task run                      # run the client locally
task run:dev                  # run with DEBUG logging

# Code quality
task lint
task format
task test

# Deploy to a physical display
task deploy -- pi@colorframe05.local        # rsync + restart
task deploy:dry -- pi@colorframe05.local    # preview changes without writing

# Interact with a running display
task logs    -- pi@colorframe05.local       # stream journald logs
task status  -- pi@colorframe05.local       # systemctl status
task restart -- pi@colorframe05.local       # restart the service
task ssh     -- pi@colorframe05.local       # open a shell
```

## Manual Service Connection Setup

When automatic discovery or bootstrap is unavailable, use the interactive helper:

```bash
./scripts/setup_connection.sh
```

It updates the display `.env` (or `/etc/mimir-display/.env` on installed systems) with:

- `PLATFORM_URL`
- `MQTT_BROKER_HOST`
- `MQTT_BROKER_PORT`
- optional `MQTT_USERNAME` / `MQTT_PASSWORD`
- optional `DISPLAY_NAME` / `DISPLAY_LOCATION`

For normal first-boot onboarding on a Linux server, leave `PLATFORM_URL` and
`MQTT_BROKER_HOST` blank and let the display discover Mimir via mDNS. Only use
manual service connection values when mDNS bootstrap is unavailable.

After saving, restart the service:

```bash
sudo systemctl restart mimir-display
```

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
| DISPLAY_BACKEND | Backend selection (`auto`, `inky`, `hyperpixelsq`, `rgbmatrix`, `hdmi`) | `DISPLAY_BACKEND=auto` |
| DISPLAY_ORIENTATION | Physical mounting: `landscape`, `portrait_left`, `portrait_right` | `DISPLAY_ORIENTATION=portrait_left` |
| STARTUP_LOGO_PATH | Override path to startup image (defaults to built-in `startup.png`) | `/opt/mimir-display/logo.png` |
| STARTUP_TEST_PATTERN | If `1` and HyperPixel backend, render diagnostic gradient before logo | `STARTUP_TEST_PATTERN=1` |
| LOG_LEVEL | Logging threshold | `INFO` |
| MQTT_BROKER_HOST / MQTT_BROKER_PORT | MQTT broker host/port | `mimir.local` / `1883` |
| DISPLAY_ID | Optional override (hostname slug used otherwise) | `DISPLAY_ID=myframe01` |
| DEFAULT_CONTENT_PATH | Fallback image to show if assignments fail | `./example_default.png` |
| HYPERPIXEL_RGB565_ENDIAN | `little` or `big` override for RGB565 writes | `HYPERPIXEL_RGB565_ENDIAN=little` |
| HYPERPIXEL_RGB565_CHANNEL | `rgb` or `bgr` channel swap for RGB565 | `HYPERPIXEL_RGB565_CHANNEL=bgr` |
| HYPERPIXEL_FORCE_BPP | Force treat framebuffer as 16/24/32 bpp | `HYPERPIXEL_FORCE_BPP=16` |
| HYPERPIXEL_8888_SEQ | 4-byte sequence for 32bpp layout (chars in `RGBAX`) | `HYPERPIXEL_8888_SEQ=BGRX` |
| HYPERPIXEL_LOG_FIRST_BYTES | Log first N bytes of first framebuffer write | `HYPERPIXEL_LOG_FIRST_BYTES=64` |
| HDMI_RESOLUTION | Force window/native size (WxH) | `HDMI_RESOLUTION=1920x1080` |
| HDMI_WINDOWED | If `1`, run in a window (not fullscreen) | `HDMI_WINDOWED=1` |
| HDMI_SCALE_MODE | `fit` (letterbox) or `fill` scale strategy | `HDMI_SCALE_MODE=fit` |
| HDMI_BG_COLOR | Hex background for letterbox (#RRGGBB) | `HDMI_BG_COLOR=#000000` |
| RGBMATRIX_ROWS | Panel rows (e.g. 32, 64) | `RGBMATRIX_ROWS=64` |
| RGBMATRIX_CHAIN_LENGTH | Daisy chained panels | `RGBMATRIX_CHAIN_LENGTH=2` |
| RGBMATRIX_PARALLEL | Parallel chains (advanced) | `RGBMATRIX_PARALLEL=1` |
| RGBMATRIX_HARDWARE_MAPPING | Hardware mapping string ("regular", "adafruit-hat", etc.) | `RGBMATRIX_HARDWARE_MAPPING=adafruit-hat` |
| RGBMATRIX_GPIO_SLOWDOWN | Timing slowdown (0-4) | `RGBMATRIX_GPIO_SLOWDOWN=2` |
| RGBMATRIX_PWM_BITS | PWM bit depth | `RGBMATRIX_PWM_BITS=11` |
| RGBMATRIX_BRIGHTNESS | Brightness 1-100 | `RGBMATRIX_BRIGHTNESS=60` |
| RGBMATRIX_LIMIT_FPS | Software limit refresh rate | `RGBMATRIX_LIMIT_FPS=30` |
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

`rgbmatrix` is not auto-detected (requires explicit `--backend rgbmatrix` or `DISPLAY_BACKEND=rgbmatrix`). This avoids
mis-identification on systems that also have a framebuffer.

`hdmi` backend is not auto-detected either; specify `--backend hdmi` or set `DISPLAY_BACKEND=hdmi`.

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

## RGB LED Matrix (HUB75) Setup

This backend uses the `hzeller/rpi-rgb-led-matrix` library (Python bindings) to drive HUB75 RGB LED panels via GPIO.

Prerequisites (follow first):
1. Adafruit Bonnet / HAT wiring and soldering complete (if using the Adafruit RGB Matrix Bonnet).
2. Follow the official setup guide: https://learn.adafruit.com/adafruit-rgb-matrix-bonnet-for-raspberry-pi/matrix-setup
3. Build & install the library (ensures `rgbmatrix` Python module importable).

Install library (external build/install first). The `rgbmatrix` extra here is a no-op placeholder so the installer can use a uniform pattern; you still must build the hzeller library yourself.
```
# Build & install hzeller/rpi-rgb-led-matrix per upstream docs
# (produces the rgbmatrix Python module)
pip install .[rgbmatrix]   # does not fetch additional packages; safe if already built
```

Run with explicit backend:
```
mimir-display --backend rgbmatrix
```

Or via environment:
```
echo 'DISPLAY_BACKEND=rgbmatrix' >> .env
```

Environment tuning (common):
```
RGBMATRIX_ROWS=64
RGBMATRIX_CHAIN_LENGTH=2
RGBMATRIX_HARDWARE_MAPPING=adafruit-hat
RGBMATRIX_BRIGHTNESS=70
```

Orientation: uses the same `DISPLAY_ORIENTATION` (landscape / portrait_left / portrait_right).

If the `rgbmatrix` module is missing or initialization fails, the client will fall back to simulation mode and log a warning.

### Optional: Suppress Console Cursor / Reduce Boot Noise

You can refine the Raspberry Pi boot console experience by editing the kernel command line. This is a *single line* file—be careful not to introduce line breaks.

Common tweaks:
* `vt.global_cursor_default=0` – hides the blinking text cursor (cleaner for kiosk displays).
* `loglevel=3` – reduce kernel message verbosity (still shows critical errors).
* `console=tty2` – keep a secondary console, while the primary framebuffer stays clean (already present in some images).

Example current line you provided (wrapped here for readability):

```
console=serial0,115200 console=tty2 root=PARTUUID=ac7fcfdd-02 rootfstype=ext4 fsck.repair=yes rootwait cfg80211.ieee80211_regdom=US vt.global_cursor_default=0
```

If `vt.global_cursor_default=0` is missing, append it *after a space* at the end. Avoid duplicate keys.

#### Steps (Raspberry Pi OS / Bookworm)
1. Backup the existing file:
	```bash
	sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak.$(date +%Y%m%d-%H%M%S)
	```
	(Older images may use `/boot/cmdline.txt`; check which exists.)
2. Open for edit (ensure it stays one line):
	```bash
	sudo nano /boot/firmware/cmdline.txt
	```
3. Append the desired flags (e.g. `vt.global_cursor_default=0 loglevel=3`).
4. Save and reboot:
	```bash
	sudo reboot
	```

#### Verification
After reboot, the blinking cursor should be gone and early boot messages minimized. If something breaks (e.g., blank screen), restore the backup:
```bash
sudo cp /boot/firmware/cmdline.txt.bak.YYYYMMDD-HHMMSS /boot/firmware/cmdline.txt
sudo reboot
```

> Never insert newline characters inside `cmdline.txt`. The kernel treats extra lines as separate/invalid command strings.

Troubleshooting:
* Run `cat /sys/class/graphics/fb0/virtual_size` → expect `720,720`.
* Run `cat /sys/class/graphics/fb0/bits_per_pixel` → expect `16`.
* If these differ, verify the overlay line and reboot again.

## Inky E‑Ink SPI Chip Select Contention (dtoverlay=spi0-0cs)

Some Raspberry Pi images (especially newer Bookworm variants or systems that previously enabled additional SPI peripherals) can leave the second chip select (CS1) on the primary SPI bus in a conflicting state. In practice this can surface when initializing an Inky e‑ink display as errors such as:

```
RuntimeError: Failed to initialise Inky: device busy / pin already in use
```

or low‑level messages about GPIO pin reservation / chip select failure. Applying the following simple Device Tree overlay constrains the SPI0 controller to a single chip select (CS0) and resolved the contention for us:

```
dtoverlay=spi0-0cs
```

### When to Apply
Apply this if:
* The Inky backend keeps failing to initialize while SPI is otherwise enabled.
* You previously had another SPI device on CS1.
* You see intermittent success only after cold boots.

### Steps
1. Determine which boot config path your image uses (Bookworm usually `/boot/firmware/config.txt`; older images `/boot/config.txt`).
2. Backup the file:
	```bash
	sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.bak.$(date +%Y%m%d-%H%M%S) || \
	sudo cp /boot/config.txt /boot/config.txt.bak.$(date +%Y%m%d-%H%M%S)
	```
3. Edit the active file and add (or ensure) a single line containing:
	```
	dtoverlay=spi0-0cs
	```
	Place it near other dtoverlay entries; avoid duplicates.
4. Reboot:
	```bash
	sudo reboot
	```

### Verification
After reboot run:
```bash
ls -l /dev/spidev0.*
dmesg | grep -i spi | head -n 20
```
You should see `/dev/spidev0.0` present and no errors about CS lines. The Inky initialization should now proceed without falling back to simulation.

### Reverting
If the change causes unexpected behavior for another SPI peripheral, remove the `dtoverlay=spi0-0cs` line (or comment it out with a leading `#`), restore your backup, and reboot.

> Rationale: Some helper scripts or prior overlays allocate both CS lines or leave CS1 asserted, which can confuse simple display drivers that expect uncontended access on CS0. Limiting to one chip select eliminates that edge case for single‑device setups like an Inky e‑ink panel.

### Legacy Pi Zero W + older Inky shields

On early Raspberry Pi Zero W devices paired with older Inky boards, the most reliable combination has been using the legacy Inky Python package series 1.5.x. If you encounter intermittent init errors or refresh failures even after applying `dtoverlay=spi0-0cs`, try pinning Inky to 1.5.0 on that device:

```
# Inside your virtualenv on the device
pip install "inky[rpi]==1.5.0"
```

Notes:
- The `[rpi]` extra pulls in RPi.GPIO and spidev automatically. Our `pyproject.toml` also declares these explicitly for the `inky` extra; either approach is fine.
- Keep your existing `.env` (e.g., `DISPLAY_BACKEND=inky`). You may also set `INKY_IGNORE_PIN_BUSY=true` if the pin guard still complains about CS0 being in use.
- On ARMv6 (Pi Zero/Zero W), prefer prebuilt wheels via PiWheels; if no wheels are available for your Python version, installation may be slow or fail. Using the installer’s defaults (PiWheels index) helps avoid source builds.

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

## Over-the-Air Updates (OTA)

Once a display has run one deploy that includes the OTA updater (installed
automatically by `update_display.sh`), it updates itself:

1. The server publishes the pinned client version on the retained MQTT topic
   `mimir/fleet/desired_version` (sourced from `versions.yml` via the server's
   release cache — displays download from the server, not the internet).
2. The (unprivileged) client evaluates it — canary-phase rollouts only apply
   to displays whose `DISPLAY_TAGS` include `canary` — and writes
   `/var/lib/mimir-display/ota/request.json`.
3. The root-owned `mimir-display-updater.path` unit fires `scripts/ota_update.sh`:
   download → sha256 verify → install into `/opt/mimir-display/releases/vX.Y.Z/`
   (own venv) → health check → flip the `current` symlink → restart → verify.
   On any failure the previous version keeps running and the failure is
   reported in the display's presence payload (visible in the web UI).

Useful bits:

```bash
journalctl -u mimir-display-updater -n 50    # updater log on the device
cat /var/lib/mimir-display/ota/status.json   # last update outcome
OTA_PIP_EXTRAS=inky                          # .env override for install extras
                                             # (defaults map from DISPLAY_BACKEND)
```

The retained topic re-delivers on every MQTT (re)connect, so displays that
were offline catch up automatically. A failed update retries on the next
reconnect after a 1-hour backoff. Rollback of the whole fleet = pin the older
version in `versions.yml`.

## Updating an Existing Installation

The simplest path from your dev machine:

```bash
task deploy -- pi@colorframe05.local
```

This rsyncs the repo to `/opt/mimir-display` (preserving the device's `.env`) and runs `update_display.sh` remotely. If you're working directly on the device, run the script manually:

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

### HDMI Backend Notes
The HDMI backend opens a fullscreen window (via `pygame` when available, falling back to `tkinter`) and renders images there. It does not directly mmap the framebuffer which keeps it portable to desktop development machines. Install with the extra:

```
pip install .[hdmi]
```

Environment quick start:

```
DISPLAY_BACKEND=hdmi
HDMI_RESOLUTION=1920x1080
HDMI_SCALE_MODE=fit
```

If you prefer running windowed while iterating locally:

```
HDMI_WINDOWED=1
```

When `fit` scaling is used and the aspect ratio of the source image differs, the image is centered and letterboxed with `HDMI_BG_COLOR` (default black). Use `fill` to stretch/crop instead (maintains full coverage without preserving aspect).

## License
MIT
