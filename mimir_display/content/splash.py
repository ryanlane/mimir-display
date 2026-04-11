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
from typing import Optional

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


def _status_bar_h(W: int, H: int) -> int:
    """Height in pixels reserved at the bottom for the status bar."""
    small_fs = max(8, min(W, H) // 28)
    return small_fs + 16


def build_splash(
    width: int,
    height: int,
    pair_code: str,
    platform_url: str | None,
    ip_address: str,
    logo_path: Optional[str] = None,
    status_text: str = "",
    qr_url: Optional[str] = None,
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
        status_text:  Optional initial status line text.
        qr_url:       Explicit URL to encode in the QR code. When provided this
                      takes precedence over the pair-code URL derived from
                      platform_url. Use this for provisioning / setup flows.

    Returns:
        RGB PIL Image ready to be saved and passed to DisplayManager.
    """
    if qr_url is not None:
        pair_url = qr_url
    else:
        pair_url = f"{platform_url.rstrip('/')}/displays?pair={pair_code}" if platform_url else None

    canvas = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(canvas)

    is_portrait = height > width * 1.1
    is_square   = abs(width - height) <= max(10, min(width, height) // 10)

    pad = max(8, min(width, height) // 20)

    # Reserve a fixed strip at the bottom for the status bar.
    # All layout functions receive H_eff (content height) and never draw below it.
    sb_h = _status_bar_h(width, height)
    H_eff = height - sb_h

    # Draw status bar background (light gray rule)
    draw.rectangle([(0, H_eff), (width, height)], fill=(235, 235, 240))
    draw.line([(0, H_eff), (width, H_eff)], fill=_RULE, width=1)

    code_fs  = max(14, min(width, H_eff) // 8)
    small_fs = max(8,  min(width, H_eff) // 28)

    code_font  = _load_font(code_fs,  bold=True)
    small_font = _load_font(small_fs, bold=False)

    logo_img = _load_logo(logo_path, width, H_eff, is_portrait, is_square, pad)
    qr_img   = _make_qr_image(pair_url, width, H_eff, is_portrait, is_square, pad)

    if is_portrait:
        _layout_portrait(canvas, draw, width, H_eff, pad,
                         logo_img, qr_img, pair_code, ip_address,
                         code_font, small_font)
    elif is_square:
        _layout_square(canvas, draw, width, H_eff, pad,
                       logo_img, qr_img, pair_code, ip_address,
                       code_font, small_font)
    else:
        _layout_landscape(canvas, draw, width, H_eff, pad,
                          logo_img, qr_img, pair_code, ip_address,
                          code_font, small_font)

    # Draw initial status text if provided
    if status_text:
        _draw_status_text(draw, width, height, sb_h, small_font, status_text, is_error=False)

    return canvas


def _draw_status_text(
    draw: ImageDraw.ImageDraw,
    W: int,
    H: int,
    sb_h: int,
    font: ImageFont.ImageFont,
    text: str,
    is_error: bool = False,
) -> None:
    """Fill the reserved status bar at the bottom with coloured text."""
    if is_error:
        bg = (180, 40, 40)
        fg = (255, 255, 255)
        draw.rectangle([(0, H - sb_h), (W, H)], fill=bg)
    else:
        fg = _ACCENT
    y = H - sb_h + (sb_h - max(8, min(W, H) // 28)) // 2
    _draw_text_centered(draw, W // 2, y, text, font, fg)


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
            max_w, max_h = W // 2 - pad * 2, H - pad * 2
        img.thumbnail((max_w, max_h), Image.LANCZOS)
        return img
    except Exception:
        return None


def _make_qr_image(
    url: str | None,
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

    if not url:
        qr_pil = Image.new("RGB", (target, target), (240, 240, 244))
        d = ImageDraw.Draw(qr_pil)
        font = _load_font(max(10, target // 9), bold=False)
        lines = ["Searching", "for", "Mimir..."]
        line_height = max(12, target // 9)
        start_y = target // 2 - (len(lines) * line_height) // 2
        for index, line in enumerate(lines):
            _draw_text_centered(d, target // 2, start_y + index * line_height, line, font, _GRAY)
        return qr_pil

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


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
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


def _fit_image(
    img: Image.Image,
    max_w: int,
    max_h: int,
    resample: int,
) -> Image.Image:
    """Return a resized copy that fits within the requested box."""
    if img.width <= max_w and img.height <= max_h:
        return img

    scale = min(max_w / img.width, max_h / img.height)
    if scale <= 0:
        return img

    new_size = (
        max(1, int(img.width * scale)),
        max(1, int(img.height * scale)),
    )
    return img.resize(new_size, resample)


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
    Left half:  logo (upper), pairing code + IP address (lower).
    Right half: QR code (centred) + scan label.
    H is the effective content height (status bar already excluded).
    """
    half = W // 2
    left_cx  = half // 2
    right_cx = half + half // 2

    # ── Measure text heights up front ────────────────────────────────────────
    code_h = _text_size(draw, code, code_font)[1]
    ip_text = "IP: " + ip
    ip_h    = _text_size(draw, ip_text, small_font)[1]
    label_h = _text_size(draw, "Scan QR or enter code to pair", small_font)[1]

    # ── Left half ─────────────────────────────────────────────────────────────
    # Reserve the bottom area for: IP text + gap + code text + pad
    bottom_block_h = ip_h + pad // 2 + code_h + pad
    logo_area_h    = H - bottom_block_h - pad
    left_area_w = half - pad * 2

    if logo_img and logo_area_h > pad * 2:
        fitted_logo = _fit_image(logo_img, left_area_w, logo_area_h, Image.LANCZOS)
        ly = pad + (logo_area_h - fitted_logo.height) // 2
        _paste_image(canvas, fitted_logo, left_cx, max(pad, ly))

    # Stack IP above code, both anchored to the bottom of the content area
    ip_y   = H - pad - code_h - pad // 2 - ip_h
    code_y = H - pad - code_h
    _draw_text_centered(draw, left_cx, ip_y,   ip_text, small_font, _GRAY)
    _draw_text_centered(draw, left_cx, code_y, code,    code_font,  _ACCENT)

    # ── Right half ────────────────────────────────────────────────────────────
    # Centre QR; place label below it, leaving at least pad above the bottom
    label_area_h = label_h + pad
    qr_area_h    = H - label_area_h - pad
    right_area_w = half - pad * 2
    fitted_qr = _fit_image(qr_img, right_area_w, qr_area_h, Image.NEAREST)
    qr_y = pad + (qr_area_h - fitted_qr.height) // 2
    _paste_image(canvas, fitted_qr, right_cx, max(pad, qr_y))

    label_y = H - pad - label_h
    _draw_text_centered(draw, right_cx, label_y, "Scan QR or enter code to pair", small_font, _GRAY)

    # ── Vertical divider ──────────────────────────────────────────────────────
    draw.line([(half, pad), (half, H - pad)], fill=_RULE, width=1)


def _layout_portrait(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    W: int, _H: int, pad: int,
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
    Update the reserved status bar at the bottom of an existing splash image.

    The status bar area was pre-allocated by build_splash, so this never
    touches the pair code or any other content above it.

    Args:
        splash_path: Path to the existing splash PNG.
        status_text: Short message to display.
        is_error:    If True, use a red background; otherwise the accent colour.

    Returns:
        Updated PIL Image, or None if the file cannot be read.
    """
    try:
        img = Image.open(splash_path).convert("RGB")
    except Exception:
        return None

    W, H = img.size
    sb_h = _status_bar_h(W, H)
    small_fs = max(8, min(W, H) // 28)
    font = _load_font(small_fs, bold=False)
    draw = ImageDraw.Draw(img)

    # Restore the neutral status bar background first, then colour if error
    draw.rectangle([(0, H - sb_h), (W, H)], fill=(235, 235, 240))
    draw.line([(0, H - sb_h), (W, H - sb_h)], fill=_RULE, width=1)
    _draw_status_text(draw, W, H, sb_h, font, status_text, is_error=is_error)
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
