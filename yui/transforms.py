"""Validated crop and colour adjustments shared by GIF and poster exports."""
from __future__ import annotations

import math

EFFECTS = {
    "none": "Original", "grayscale": "Grayscale", "sepia": "Sepia",
    "vivid": "Vivid", "warm": "Warm", "cool": "Cool", "invert": "Invert",
}
PALETTE_MODES = {"auto": "Automatic", "full": "Full scene"}
# Colour matrices run in display RGB in both encoders. Values are constants,
# never expressions supplied by a project file or a text field.
_MATRICES = {
    "grayscale": (.299, .587, .114, .299, .587, .114, .299, .587, .114),
    "sepia": (.393, .769, .189, .349, .686, .168, .272, .534, .131),
    "vivid": (1.2103, -.1761, -.0342, -.0897, 1.1239, -.0342, -.0897, -.1761, 1.2658),
    "warm": (1.06, 0, 0, 0, 1, 0, 0, 0, .90),
    "cool": (.90, 0, 0, 0, 1, 0, 0, 0, 1.08),
}


def _error(message):
    from .engine import MediaError
    return MediaError(message)


def validate_edit_options(opts):
    """Validate and normalise edit fields in place, then return the options.

    A full-canvas crop becomes None so the unedited path stays byte-identical.
    This function opens no files and never allocates an image.
    """
    crop = getattr(opts, "crop", None)
    if crop is not None:
        if not isinstance(crop, (tuple, list)) or len(crop) != 4:
            raise _error("The crop needs left, top, width and height values.")
        values = []
        for value in crop:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _error("Crop coordinates must be numbers between zero and one.")
            try:
                value = float(value)
            except (ValueError, OverflowError):
                raise _error("The crop coordinates are outside the supported range.") from None
            if not math.isfinite(value):
                raise _error("The crop coordinates must be finite numbers.")
            values.append(value)
        left, top, width, height = values
        if (not 0 <= left < 1 or not 0 <= top < 1 or not 0 < width <= 1
                or not 0 < height <= 1 or left + width > 1 + 1e-9 or top + height > 1 + 1e-9):
            raise _error("The crop must stay inside the source image.")
        crop = (left, top, min(width, 1 - left), min(height, 1 - top))
        if crop == (0., 0., 1., 1.):
            crop = None
    effect = getattr(opts, "effect", "none")
    if not isinstance(effect, str) or effect not in EFFECTS:
        raise _error("Choose one of the available colour effects.")
    reverse = getattr(opts, "reverse", False)
    if not isinstance(reverse, bool):
        raise _error("Reverse must be switched on or off.")
    colors = getattr(opts, "palette_colors", 256)
    if isinstance(colors, bool) or not isinstance(colors, int) or not 4 <= colors <= 256:
        raise _error("Choose a palette containing 4 to 256 colours.")
    mode = getattr(opts, "palette_mode", "auto")
    if not isinstance(mode, str) or mode not in PALETTE_MODES:
        raise _error("Choose an available palette analysis mode.")
    opts.crop = crop
    opts.effect = effect
    opts.reverse = reverse
    opts.palette_colors = colors
    opts.palette_mode = mode
    return opts


def crop_pixels(size, crop):
    """Return a bounded PIL crop box using the displayed, EXIF-correct size."""
    width, height = size
    if crop is None:
        return (0, 0, width, height)
    left, top, fraction_w, fraction_h = crop
    x = min(width - 1, max(0, round(left * width)))
    y = min(height - 1, max(0, round(top * height)))
    right = min(width, max(x + 1, round((left + fraction_w) * width)))
    bottom = min(height, max(y + 1, round((top + fraction_h) * height)))
    return (x, y, right, bottom)


def image_transform(image, opts):
    """Crop and colour a decoded image without retaining source metadata."""
    from PIL import ImageOps
    validate_edit_options(opts)
    if opts.crop is not None:
        image = image.crop(crop_pixels(image.size, opts.crop))
    if opts.effect == "none":
        return image
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    rgb = image.convert("RGB")
    if opts.effect == "invert":
        rgb = ImageOps.invert(rgb)
    else:
        matrix = _MATRICES[opts.effect]
        rgb = rgb.convert("RGB", tuple(value for row in range(3) for value in (*matrix[row * 3:row * 3 + 3], 0)))
    if alpha is not None:
        rgb.putalpha(alpha)
    rgb.info.clear()
    return rgb


def ffmpeg_filters(opts, width, height):
    """Return a safe crop/effect chain; an unedited source returns no filters."""
    validate_edit_options(opts)
    filters = []
    if opts.crop is not None:
        left, top, right, bottom = crop_pixels((width, height), opts.crop)
        # RGB conversion before an exact crop supports odd pixel coordinates
        # consistently, including 4:2:0 video and a one-pixel crop.
        filters += ["format=rgba", f"crop={right-left}:{bottom-top}:{left}:{top}:exact=1"]
    if opts.effect != "none":
        filters.append("format=rgba")
        if opts.effect == "invert":
            filters.append("lutrgb=r=negval:g=negval:b=negval")
        else:
            names = ("rr", "rg", "rb", "gr", "gg", "gb", "br", "bg", "bb")
            values = ":".join(f"{name}={value:g}" for name, value in zip(names, _MATRICES[opts.effect]))
            filters.append("colorchannelmixer=" + values)
    return filters
