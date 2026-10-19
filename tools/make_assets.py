"""Draw the Yui Gif Converter icon, logo and splash screen into assets/."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
PINK = (255, 107, 157)
VIOLET = (124, 92, 255)


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    names = ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")
    for name in names:
        if (fonts / name).exists():
            return ImageFont.truetype(str(fonts / name), size=size)
    return ImageFont.load_default()


def _gradient(size: int) -> Image.Image:
    """Diagonal pink → violet gradient."""
    grad = Image.new("RGB", (size, size))
    px = grad.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1) or 1)
            px[x, y] = tuple(int(PINK[i] + (VIOLET[i] - PINK[i]) * t) for i in range(3))
    return grad


def draw_mark(size: int) -> Image.Image:
    """Rounded gradient tile with a white "Y" and a small sparkle."""
    scale = 4  # draw large, then downsample for smooth edges
    s = size * scale
    tile = _gradient(s).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    margin = round(s * 0.04)
    ImageDraw.Draw(mask).rounded_rectangle(
        [margin, margin, s - 1 - margin, s - 1 - margin], radius=round(s * 0.23), fill=255
    )
    out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    out.paste(tile, (0, 0), mask)

    d = ImageDraw.Draw(out)
    font = _font(round(s * 0.62))
    text = "Y"
    box = d.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = (s - tw) / 2 - box[0] - s * 0.02
    y = (s - th) / 2 - box[1] + s * 0.02
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x, y + s * 0.02), text, font=font, fill=(60, 20, 90, 90))
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(s * 0.015)))
    d = ImageDraw.Draw(out)
    d.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    if size >= 24:
        # Four-point sparkle, top right.
        cx, cy, r = s * 0.74, s * 0.27, s * 0.11
        w = r * 0.28
        d.polygon(
            [(cx, cy - r), (cx + w, cy - w), (cx + r, cy), (cx + w, cy + w),
             (cx, cy + r), (cx - w, cy + w), (cx - r, cy), (cx - w, cy - w)],
            fill=(255, 255, 255, 235),
        )
    return out.resize((size, size), Image.LANCZOS)


def make_icon() -> Path:
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    images = [draw_mark(s) for s in sizes]
    path = ASSETS / "icon.ico"
    images[-1].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
    return path


def make_logo() -> Path:
    path = ASSETS / "logo.png"
    draw_mark(256).save(path)
    return path


def make_splash() -> Path:
    w, h = 520, 300
    img = Image.new("RGB", (w, h), (13, 12, 16))
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([w * 0.5 - 220, -160, w * 0.5 + 220, 200], fill=PINK + (60,))
    img = Image.alpha_composite(img.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(70)))
    mark = draw_mark(96)
    img.alpha_composite(mark, ((w - 96) // 2, 58))
    d = ImageDraw.Draw(img)
    title = "Yui Gif Converter"
    f1 = _font(30)
    tb = d.textbbox((0, 0), title, font=f1)
    d.text(((w - (tb[2] - tb[0])) / 2 - tb[0], 176), title, font=f1, fill=(244, 242, 247))
    sub = "Starting up…"
    f2 = _font(14, bold=False)
    sb = d.textbbox((0, 0), sub, font=f2)
    d.text(((w - (sb[2] - sb[0])) / 2 - sb[0], 222), sub, font=f2, fill=(154, 149, 166))
    path = ASSETS / "splash.png"
    img.convert("RGB").save(path)
    return path


if __name__ == "__main__":
    ASSETS.mkdir(exist_ok=True)
    for made in (make_icon(), make_logo(), make_splash()):
        print("wrote", made.relative_to(ROOT))
