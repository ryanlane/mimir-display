"""
Dynamic startup splash screen generator.

Builds a PIL image showing:
  - Mimir logo
  - QR code linking to the pairing URL
  - 6-character pairing code (large, monospace)
  - Device IP address

Supports landscape, portrait, and square display orientations.
"""
from __future__ import annotations

import os
import secrets
import socket
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# Unambiguous alphabet — no 0, 1, I, L, O
PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

# ── Colour palette ─────────────────────────────────────────────────────────────
_BG     = (255, 255, 255)
_FG     = (20,  20,  20)
_ACCENT = (80,  60,  200)   # purple accent matching the web UI
_GRAY   = (130, 130, 130)
_RULE   = (200, 200, 210)


# ── Public helpers ─────────────────────────────────────────────────────────────

def generate_pair_code(length: int = 6) -> str:
    """Generate a random pairing code using the unambiguous alphabet."""
    return "".join(secrets.choice(PAIR_ALPHABET) for _ in range(length))


def get_local_ip() -> str:
    """Return the device's primary outbound IP address, or 'Unknown IP'."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip: str = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "Unknown IP"


def build_splash(
    width: int,
    height: int,
    pair_code: str,
    platform_url: str,
    ip_address: str,
    logo_path: Optional[str] = None,
) -> Image.Image:
    """
    Compose and return a startup splash PIL Image.

    Args:
        width:        Canvas width in pixels.
        height:       Canvas height in pixels.
        pair_code:    6-character pairing code to display.
        platform_url: Base URL of the Mimir server (used to build the QR URL).
        ip_address:   IP address string to display.
        logo_path:    Optional path to the logo PNG/JPEG.

    Returns:
        RGB PIL Image ready to be saved and passed to DisplayManager.
    """
    pair_url = f"{platform_url.rstrip('/')}/displays?pair={pair_code}"

    canvas = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(canvas)

    is_portrait = height > width * 1.1
    is_square   = abs(width - height) <= max(10, min(width, height) // 10)

    pad = max(8, min(width, height) // 20)

    logo_img = _load_logo(logo_path, width, height, is_portrait, is_square, pad)
    qr_img   = _make_qr_image(pair_url, width, height, is_portrait, is_square, pad)

    code_fs  = max(14, min(width, height) // 8)
    small_fs = max(8,  min(width, height) // 28)

    code_font  = _load_font(code_fs,  bold=True)
    small_font = _load_font(small_fs, bold=False)

    if is_portrait:
        _layout_portrait(canvas, draw, width, height, pad,
                         logo_img, qr_img, pair_code, ip_address,
                         code_font, small_font)
    elif is_square:
        _layout_square(canvas, draw, width, height, pad,
                       logo_img, qr_img, pair_code, ip_address,
                       code_font, small_font)
    else:
        _layout_landscape(canvas, draw, width, height, pad,
                          logo_img, qr_img, pair_code, ip_address,
                          code_font, small_font)

    return canvas


# ── Internal helpers ──────────────────────────────────────────────────────────

def _load_logo(
    logo_path: Optional[str],
    W: int, H: int,
    is_portrait: bool, is_square: bool,
    pad: int,
) -> Optional[Image.Image]:
    if not logo_path or not os.path.exists(logo_path):
        return None
    try:
        img = Image.open(logo_path).convert("RGBA")
        if is_portrait:
            max_w, max_h = W - pad * 2, H // 5
        elif is_square:
            max_w, max_h = W // 2 - pad * 2, H // 4
        else:
            max_w, max_h = W // 3 - pad * 2, H // 2 - pad * 4
        img.thumbnail((max_w, max_h), Image.LANCZOS)
        return img
    except Exception:
        return None


def _make_qr_image(
    url: str,
    W: int, H: int,
    is_portrait: bool, is_square: bool,
    pad: int,
) -> Image.Image:
    if is_portrait:
        target = min(W - pad * 4, H // 3)
    elif is_square:
        target = min(W // 2 - pad * 2, H // 3)
    else:
        target = min(H - pad * 6, W // 3 - pad * 2)

    target = max(80, target)
    box_size = max(2, target // 25)

    try:
        import qrcode  # type: ignore
        import qrcode.constants  # type: ignore

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=box_size,
            border=2,
        )
        qr.add_data(url)
        qr.make(fit=True)
        qr_pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except ImportError:
        # Placeholder if qrcode package is not installed
        qr_pil = Image.new("RGB", (target, target), (230, 230, 230))
        d = ImageDraw.Draw(qr_pil)
        d.text((target // 6, target // 2 - 8), "qrcode\nnot installed", fill=_GRAY)

    # Scale to consistent target size (NEAREST preserves crisp modules)
    qr_pil = qr_pil.resize((target, target), Image.NEAREST)
    return qr_pil


def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf" if bold
            else "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" if bold
            else "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> Tuple[int, int]:
    """Pillow-version-agnostic text size helper."""
    try:
        bb = draw.textbbox((0, 0), text, font=font)
        return bb[2] - bb[0], bb[3] - bb[1]
    except AttributeError:
        return draw.textsize(text, font=font)  # type: ignore[attr-defined]


def _draw_text_centered(
    draw: ImageDraw.ImageDraw,
    x_center: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple,
) -> int:
    """Draw text horizontally centred at x_center, top-aligned at y. Returns text height."""
    tw, th = _text_size(draw, text, font)
    draw.text((x_center - tw // 2, y), text, font=font, fill=fill)
    return th


def _paste_image(
    canvas: Image.Image,
    img: Image.Image,
    x_center: int,
    y: int,
) -> int:
    """Paste img centred on x_center with its top at y. Returns img height."""
    x = x_center - img.width // 2
    if img.mode == "RGBA":
        canvas.paste(img, (x, y), img)
    else:
        canvas.paste(img, (x, y))
    return img.height


# ── Layout implementations ─────────────────────────────────────────────────────

def _layout_landscape(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    W: int, H: int, pad: int,
    logo_img: Optional[Image.Image],
    qr_img: Image.Image,
    code: str, ip: str,
    code_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    """
    Left half: logo (vertically centered).
    Right half: QR code (vertically centered).
    Vertical centre divider.
    Bottom strip: pairing code | IP address (centred in respective halves).
    """
    half = W // 2

    # Left — logo
    if logo_img:
        ly = (H - logo_img.height) // 2
        _paste_image(canvas, logo_img, half // 2, max(pad, ly))

    # Right — QR code
    qr_y = (H - qr_img.height) // 2
    _paste_image(canvas, qr_img, half + half // 2, max(pad, qr_y))

    # Divider
    draw.line([(half, pad * 2), (half, H - pad * 2)], fill=_RULE, width=1)

    # Bottom: code centred left, IP centred right
    code_h  = _text_size(draw, code, code_font)[1]
    ip_h    = _text_size(draw, ip,   small_font)[1]

    code_y = H - pad - code_h
    ip_y   = H - pad - ip_h

    # If both fit side by side in the bottom strip:
    _draw_text_centered(draw, half // 2,        code_y, code, code_font,  _ACCENT)
    _draw_text_centered(draw, half // 2,        ip_y - code_h - pad // 2, "IP: " + ip, small_font, _GRAY)
    _draw_text_centered(draw, half + half // 2, ip_y,   "Scan QR or enter code to pair", small_font, _GRAY)


def _layout_portrait(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    W: int, H: int, pad: int,
    logo_img: Optional[Image.Image],
    qr_img: Image.Image,
    code: str, ip: str,
    code_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    """
    Top to bottom:
      Logo → rule → QR code → 'Scan or enter code' → code (large) → IP
    """
    y = pad

    # Logo
    if logo_img:
        y += _paste_image(canvas, logo_img, W // 2, y) + pad

    # Horizontal rule
    draw.line([(pad * 2, y), (W - pad * 2, y)], fill=_RULE, width=1)
    y += pad

    # QR
    y += _paste_image(canvas, qr_img, W // 2, y) + pad // 2

    # Label
    y += _draw_text_centered(draw, W // 2, y,
                             "Scan QR or enter code to pair",
                             small_font, _GRAY) + pad // 2

    # Pair code
    y += _draw_text_centered(draw, W // 2, y, code, code_font, _ACCENT) + pad // 2

    # IP
    _draw_text_centered(draw, W // 2, y, "IP: " + ip, small_font, _GRAY)


def overlay_status(
    splash_path: str,
    status_text: str,
    is_error: bool = False,
) -> Optional[Image.Image]:
    """
    Load an existing splash image and overlay a status banner at the bottom.

    Args:
        splash_path: Path to the existing splash PNG.
        status_text: Short message to display in the banner.
        is_error:    If True, use a red banner; otherwise green.

    Returns:
        Updated PIL Image, or None if the file cannot be read.
    """
    try:
        img = Image.open(splash_path).convert("RGB")
    except Exception:
        return None

    W, H = img.size
    draw = ImageDraw.Draw(img)
    small_fs = max(8, min(W, H) // 28)
    font = _load_font(small_fs, bold=False)

    banner_h = small_fs + 12
    banner_color = (180, 40, 40) if is_error else (30, 110, 50)
    draw.rectangle([(0, H - banner_h), (W, H)], fill=banner_color)
    _draw_text_centered(draw, W // 2, H - banner_h + 6, status_text, font, (255, 255, 255))
    return img


def _layout_square(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    W: int, H: int, pad: int,
    logo_img: Optional[Image.Image],
    qr_img: Image.Image,
    code: str, ip: str,
    code_font: ImageFont.ImageFont,
    small_font: ImageFont.ImageFont,
) -> None:
    """
    Top-left quadrant:  logo.
    Top-right quadrant: QR code.
    Horizontal rule at mid-height.
    Bottom half: code (large, centred) → label → IP.
    """
    half = W // 2
    mid_y = H // 2

    # Logo — centred in top-left quadrant
    if logo_img:
        lx_center = half // 2
        ly = (mid_y - logo_img.height) // 2
        _paste_image(canvas, logo_img, lx_center, max(pad, ly))

    # QR — centred in top-right quadrant
    qr_x_center = half + half // 2
    qr_y = (mid_y - qr_img.height) // 2
    _paste_image(canvas, qr_img, qr_x_center, max(pad, qr_y))

    # Horizontal rule
    draw.line([(pad, mid_y), (W - pad, mid_y)], fill=_RULE, width=1)

    y = mid_y + pad

    # Code
    y += _draw_text_centered(draw, W // 2, y, code, code_font, _ACCENT) + pad // 2

    # Label
    y += _draw_text_centered(draw, W // 2, y,
                             "Scan QR or enter code to pair",
                             small_font, _GRAY) + pad // 2

    # IP
    _draw_text_centered(draw, W // 2, y, "IP: " + ip, small_font, _GRAY)
