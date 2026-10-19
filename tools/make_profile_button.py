"""Render the creator button from its bundled avatar, matching the Ko-fi artwork size."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def main():
    with Image.open(ASSETS / "kofi-button.png") as reference:
        width, height = reference.size
    scale = height / 198
    with Image.open(ASSETS / "juicychat-avatar.png") as source:
        avatar = ImageOps.fit(source.convert("RGB"), (256, 256), method=Image.Resampling.LANCZOS)
    # Keep a small, metadata-free offline copy; rendering never contacts the site.
    avatar.info.clear()
    avatar.save(ASSETS / "juicychat-avatar.png", "PNG")
    image = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, width-1, height-1), radius=round(60 * scale), fill="#7448C6")
    side = round(126 * scale)
    icon = ImageOps.fit(avatar, (side, side), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", icon.size)
    ImageDraw.Draw(mask).ellipse((0, 0, side-1, side-1), fill=255)
    image.paste(icon, (round(58 * scale), (height-side)//2), mask)
    font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", round(65 * scale))
    draw.text((round(210 * scale), height//2), "Yui on JuicyChat", font=font,
              fill="white", anchor="lm")
    image.save(ASSETS / "juicychat-button.png", "PNG")
    print("Rendered the local creator button.")


if __name__ == "__main__":
    main()
