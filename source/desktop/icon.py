"""The app icon, drawn with Pillow: the web pages' favicon (blue rounded square, white play
triangle). Used for the tray icon and, by packaging/make_icons.py, for the .ico / .icns."""

from __future__ import annotations

from PIL import Image, ImageDraw

BLUE = (37, 99, 235, 255)  # --accent in source/web/app.css
GREY = (107, 114, 128, 255)  # hub stopped / error
RED = (220, 38, 38, 255)


def draw_icon(size: int = 256, color: tuple[int, int, int, int] = BLUE,
              badge: tuple[int, int, int, int] | None = None) -> Image.Image:
    """Same shapes as the favicon's SVG (viewBox 32: rect rx=7, path M12 9v14l11-7z),
    drawn 4x larger then scaled down for smooth edges. ``badge`` adds a dot (bottom right)."""
    scale = 4
    big = size * scale
    unit = big / 32
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=round(7 * unit), fill=color)
    draw.polygon([(12 * unit, 9 * unit), (12 * unit, 23 * unit), (23 * unit, 16 * unit)], fill=(255, 255, 255, 255))
    if badge is not None:
        r = 6 * unit
        cx, cy = big - r - unit * 0.5, big - r - unit * 0.5
        draw.ellipse((cx - r - unit, cy - r - unit, cx + r + unit, cy + r + unit), fill=(255, 255, 255, 255))
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=badge)
    return img.resize((size, size), Image.LANCZOS)
