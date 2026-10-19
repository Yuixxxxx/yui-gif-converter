"""Local, bounded poster layers shared by the designer and the GIF encoder.

Positions are layer centres in canvas coordinates. A text size is its font
height relative to the short side; an image size is its longest side. Stickers
use their first frame, so designs stay in place while the source GIF moves.
"""

from __future__ import annotations

import io
import math
import os
import re
import tempfile
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .paths import get_ffmpeg, is_local_path, temp_dir

MAX_LAYERS = 20
MAX_TEXT_LENGTH = 2_000
MAX_TOTAL_TEXT = 16_000
MAX_STICKER_BYTES = 32 * 1024 * 1024
MAX_STICKER_PIXELS = 16_000_000
MAX_OVERLAY_SIDE = 4_096
MAX_OVERLAY_PIXELS = 16_000_000
MAX_TILE_SIDE = 8_192
MAX_TILE_PIXELS = 16_000_000
FONTS = ("Segoe UI", "Arial", "Georgia", "Times New Roman", "Consolas")
STICKER_EXTS = frozenset((".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".ico"))


@dataclass(frozen=True)
class OverlayLayer:
    kind: str = "text"
    x: float = .5
    y: float = .5
    size: float = .10
    text: str = "Your text"
    color: str = "#ffffff"
    font: str = "Segoe UI"
    bold: bool = True
    outline: bool = True
    rotation: float = 0
    opacity: float = 1
    image: Path | None = None


def _error(message: str):
    from .engine import MediaError
    return MediaError(message)


def _limit_error(message: str):
    from .engine import MediaLimitError
    return MediaLimitError(message)


def _cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        from .engine import RenderCancelled
        raise RenderCancelled()


def _number(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(f"The layer {name} must be a number.")
    try:
        value = float(value)
    except (ValueError, OverflowError):
        raise _error(f"The layer {name} is outside the supported range.") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise _error(f"The layer {name} is outside the supported range.")
    return value


def validate_layers(layers) -> tuple[OverlayLayer, ...]:
    """Reject invalid designs before opening any files or allocating a raster."""
    if not isinstance(layers, (tuple, list)) or len(layers) > MAX_LAYERS:
        raise _error(f"A design can contain up to {MAX_LAYERS} text or image layers.")
    result = []
    total_text = 0
    for layer in layers:
        if not isinstance(layer, OverlayLayer) or layer.kind not in ("text", "image"):
            raise _error("The design contains an unsupported layer.")
        x, y = _number(layer.x, "position", 0, 1), _number(layer.y, "position", 0, 1)
        size = _number(layer.size, "size", .01, 1.5)
        rotation = _number(layer.rotation, "rotation", -180, 180)
        opacity = _number(layer.opacity, "opacity", 0, 1)
        if not isinstance(layer.bold, bool) or not isinstance(layer.outline, bool):
            raise _error("The design contains an unsupported text style.")
        if not isinstance(layer.font, str) or layer.font not in FONTS:
            raise _error("Choose one of the available fonts.")
        if not isinstance(layer.color, str) or not re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", layer.color):
            raise _error("Choose a valid text colour.")
        if not isinstance(layer.text, str) or len(layer.text) > MAX_TEXT_LENGTH:
            raise _error(f"Each text layer can contain up to {MAX_TEXT_LENGTH} characters.")
        # Newlines and tabs are supported. Other controls and lone surrogates
        # are invalid display text and can upset a font decoder.
        if any((ord(c) < 32 and c not in "\n\r\t") or 0xD800 <= ord(c) <= 0xDFFF for c in layer.text):
            raise _error("The text contains unsupported control characters.")
        total_text += len(layer.text)
        if total_text > MAX_TOTAL_TEXT:
            raise _error("This design contains too much text. Use fewer or shorter text layers.")
        image = layer.image
        if layer.kind == "image":
            if not isinstance(image, (str, Path)):
                raise _error("Choose a local image for the sticker layer.")
            image = Path(image)
            # Validate without resolve/stat/open: a network path can initiate
            # Windows authentication before a decoder ever sees it.
            if not is_local_path(image) or image.suffix.lower() not in STICKER_EXTS:
                raise _error("Stickers must be supported images on a local drive.")
        elif image is not None:
            raise _error("Text layers cannot contain an image path.")
        result.append(replace(layer, x=x, y=y, size=size, rotation=rotation, opacity=opacity, image=image))
    return tuple(result)


def _canvas_size(canvas_size) -> tuple[int, int]:
    if (not isinstance(canvas_size, (tuple, list)) or len(canvas_size) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in canvas_size)):
        raise _error("Choose a valid design canvas.")
    width, height = canvas_size
    if min(width, height) < 1 or max(width, height) > MAX_OVERLAY_SIDE or width * height > MAX_OVERLAY_PIXELS:
        raise _limit_error("The design canvas is too large. Use a canvas up to 4096 pixels per side.")
    return width, height


@lru_cache(maxsize=32)
def _font(name: str, bold: bool, size: int, text: str = ""):
    from PIL import ImageFont
    files = {
        "Segoe UI": ("segoeui.ttf", "segoeuib.ttf"),
        "Arial": ("arial.ttf", "arialbd.ttf"),
        "Georgia": ("georgia.ttf", "georgiab.ttf"),
        "Times New Roman": ("times.ttf", "timesbd.ttf"),
        "Consolas": ("consola.ttf", "consolab.ttf"),
    }
    # GetWindowsDirectory, rather than a user-supplied font path or an
    # environment override. Pillow never searches a chosen arbitrary file.
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        if 0 < length < len(buffer):
            folder = Path(buffer.value) / "Fonts"
            # Pillow does not apply Windows' automatic CJK font fallback.
            # Select a known Windows CJK face for mixed CJK strings when one
            # is installed, keeping all lookup filenames on our allowlist.
            cjk = any(0x3000 <= ord(char) <= 0x9FFF or 0xAC00 <= ord(char) <= 0xD7AF for char in text)
            candidates = (("msyhbd.ttc" if bold else "msyh.ttc", "YuGothB.ttc" if bold else "YuGothM.ttc") if cjk else ())
            for filename in (*candidates, files[name][int(bold)], "segoeui.ttf"):
                path = folder / filename
                if is_local_path(path):
                    try:
                        return ImageFont.truetype(str(path), size=size)
                    except (OSError, ValueError):
                        pass
    # Pillow's bundled font works offline even on systems missing Windows
    # fonts. Its glyph coverage can differ from the selected Windows font.
    return ImageFont.load_default(size=size)


def _wrap(text: str, font, max_width: int) -> str:
    """Wrap long lines by pixel width without losing Unicode or newlines."""
    from PIL import ImageDraw, Image
    measure = ImageDraw.Draw(Image.new("L", (1, 1)))
    wrapped = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n"):
        remaining = line
        if not remaining:
            wrapped.append("")
        while remaining:
            if measure.textlength(remaining, font=font) <= max_width:
                wrapped.append(remaining)
                break
            low, high = 1, len(remaining)
            while low < high:
                middle = (low + high + 1) // 2
                if measure.textlength(remaining[:middle], font=font) <= max_width:
                    low = middle
                else:
                    high = middle - 1
            count = max(1, low)
            space = remaining.rfind(" ", 0, count + 1)
            if space > count // 2:
                count = space
            wrapped.append(remaining[:count].rstrip())
            remaining = remaining[count:].lstrip(" ")
            if len(wrapped) > 128:
                raise _limit_error("This text is too tall for the canvas. Use a smaller size or shorter text.")
        if len(wrapped) > 128:
            raise _limit_error("This text is too tall for the canvas. Use a smaller size or shorter text.")
    return "\n".join(wrapped)


def _tile_size(width: int, height: int) -> None:
    if min(width, height) < 1 or max(width, height) > MAX_TILE_SIDE or width * height > MAX_TILE_PIXELS:
        raise _limit_error("This layer is too large. Use a smaller size or shorter text.")


@lru_cache(maxsize=4)
def _decoded_sticker(filename: str, size: int, modified: int, inode: int):
    """Keep at most four bounded decoded thumbnails for interactive dragging."""
    from PIL import Image, ImageOps
    from .engine import _open_image
    path = Path(filename)
    if not is_local_path(path):
        raise _error("Choose an image on a local drive.")
    try:
        # Snapshot the compressed input, then decode those exact bytes. A file
        # changed between validation and read cannot become an unchecked image.
        with path.open("rb") as stream:
            payload = stream.read(MAX_STICKER_BYTES + 1)
        if not payload or len(payload) > MAX_STICKER_BYTES:
            raise _error("Choose a sticker image under 32 MB.")
        with _open_image(io.BytesIO(payload)) as source:
            if source.width * source.height > MAX_STICKER_PIXELS:
                raise _limit_error("This sticker canvas is too large. Resize it to under 16 megapixels.")
            source.seek(0)
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail((2400, 2400), Image.Resampling.LANCZOS)
        image.info.clear()
        return image
    except (OSError, ValueError) as exc:
        raise _error("This sticker could not be opened. Choose another image.") from exc


def _sticker(layer: OverlayLayer, longest: int, *, upscale: bool = True):
    from PIL import Image
    path = layer.image
    if path is None or not is_local_path(path):
        raise _error("Choose an image on a local drive.")
    try:
        info = path.stat()
        if not path.is_file() or not 0 < info.st_size <= MAX_STICKER_BYTES:
            raise _error("Choose a sticker image under 32 MB.")
        image = _decoded_sticker(str(path), info.st_size, info.st_mtime_ns, info.st_ino)
        if not upscale:
            longest = min(longest, max(image.size))
        ratio = longest / max(image.size)
        size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
        _tile_size(*size)
        return image.resize(size, Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise _error("This sticker could not be opened. Choose another image.") from exc


def render_layer(layer: OverlayLayer, canvas_size) -> tuple[object, tuple[int, int, int, int]]:
    """Return a full RGBA tile and its unclipped placement bounds."""
    from PIL import Image, ImageDraw, ImageColor
    layer = validate_layers((layer,))[0]
    width, height = _canvas_size(canvas_size)
    pixels = max(1, round(layer.size * min(width, height)))
    if layer.kind == "image":
        tile = _sticker(layer, pixels)
    else:
        font = _font(layer.font, layer.bold, pixels, layer.text)
        text = _wrap(layer.text, font, max(1, round(width * .92)))
        stroke = max(1, round(pixels * .055)) if layer.outline else 0
        spacing = max(1, round(pixels * .22))
        padding = max(2, stroke + 2)
        measure = ImageDraw.Draw(Image.new("L", (1, 1)))
        box = measure.multiline_textbbox((0, 0), text, font=font, spacing=spacing,
                                        stroke_width=stroke, align="center")
        size = (max(1, math.ceil(box[2] - box[0]) + 2 * padding),
                max(1, math.ceil(box[3] - box[1]) + 2 * padding))
        _tile_size(*size)
        tile = Image.new("RGBA", size, (0, 0, 0, 0))
        ImageDraw.Draw(tile).multiline_text((padding - box[0], padding - box[1]), text, font=font,
                                            fill=ImageColor.getrgb(layer.color) + (255,), spacing=spacing,
                                            stroke_width=stroke, stroke_fill=(0, 0, 0, 255), align="center")
    if abs(layer.rotation) > .001:
        radians = math.radians(layer.rotation)
        rw = math.ceil(abs(tile.width * math.cos(radians)) + abs(tile.height * math.sin(radians))) + 2
        rh = math.ceil(abs(tile.width * math.sin(radians)) + abs(tile.height * math.cos(radians))) + 2
        _tile_size(rw, rh)
        tile = tile.rotate(-layer.rotation, resample=Image.Resampling.BICUBIC, expand=True)
    if layer.opacity < 1:
        tile.putalpha(tile.getchannel("A").point(lambda value: round(value * layer.opacity)))
    left = round(width * layer.x - tile.width / 2)
    top = round(height * layer.y - tile.height / 2)
    return tile, (left, top, left + tile.width, top + tile.height)


def compose_overlay(layers, canvas_size, should_cancel=None):
    from PIL import Image
    _cancel(should_cancel)
    layers = validate_layers(layers)
    width, height = _canvas_size(canvas_size)
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for layer in layers:
        _cancel(should_cancel)
        tile, box = render_layer(layer, (width, height))
        _cancel(should_cancel)
        canvas.alpha_composite(tile, (box[0], box[1]))
    _cancel(should_cancel)
    return canvas


def snapshot_layers(layers, folder: Path, should_cancel=None, *, max_side: int = 2400,
                    max_bytes: int = 128 * 1024 * 1024) -> tuple[OverlayLayer, ...]:
    """Freeze static sticker frames once for all GIF palette/encoding passes."""
    layers = validate_layers(layers)
    if not is_local_path(folder):
        raise _error("A local temporary folder is required for the design.")
    folder.mkdir(parents=True, exist_ok=True)
    result = []
    written = 0
    for index, layer in enumerate(layers):
        _cancel(should_cancel)
        if layer.kind == "image":
            # PNG snapshots remove source metadata and avoid re-decoding the
            # original on every sizing pass. The decoder remains bounded.
            image = _sticker(layer, max_side, upscale=False)
            path = folder / f"sticker_{index:02d}.png"
            image.info.clear()
            image.save(path, format="PNG", compress_level=1)
            written += path.stat().st_size
            if written > max_bytes:
                raise _limit_error("These stickers need too much temporary space. Use fewer or smaller images.")
            layer = replace(layer, image=path)
        result.append(layer)
    _cancel(should_cancel)
    return tuple(result)


def _frame_offset(item, offset: float) -> float:
    offset = _number(offset, "preview time", 0, 86_400)
    if not item.is_video:
        return 0
    # A frame at the exact end is unavailable, so keep the seek within trim.
    fps = item.fps if math.isfinite(item.fps) and item.fps > 0 else 25
    # Keep the seek before the last frame's start. A fixed 40 ms margin can
    # land beyond the final frame of 16/20 fps clips and produce no preview.
    margin = .001 if item.decoder == "frames" else max(.04, 1 / fps) + .001
    return item.trim_start + min(offset, max(0, item.clip_seconds - margin))


def preview_frame(item, opts, canvas_size, *, offset=0, should_cancel=None, proc_holder=None):
    """Decode one trimmed source frame with the same fit/fill/background."""
    from PIL import Image, ImageOps, ImageColor
    from .engine import (_open_image, _check_canvas, _run_capture, _hex_color,
                         MAX_ANIMATION_FRAMES, MediaError)
    from .transforms import validate_edit_options, ffmpeg_filters, image_transform
    validate_edit_options(opts)
    _cancel(should_cancel)
    width, height = _canvas_size(canvas_size)
    source = Path(item.path)
    if not is_local_path(source):
        raise _error("Yui only opens media on local drives.")
    if opts.reverse and item.is_video:
        offset = max(0, item.clip_seconds - _number(offset, "preview time", 0, 86_400))
    timestamp = _frame_offset(item, offset)
    if not source.is_file():
        raise _error("The source image or video is unavailable.")
    if item.is_video and item.decoder == "ffmpeg":
        _check_canvas(item.width, item.height)
        mode = "increase" if opts.fit == "fill" else "decrease"
        scale = ",".join((*ffmpeg_filters(opts, item.width, item.height),
                          f"scale=w={width}:h={height}:force_original_aspect_ratio={mode}:flags=lanczos"))
        if opts.fit == "fill":
            scale += f",crop={width}:{height}"
        # Write one bounded scaled frame instead of relying on the process
        # stdout capture limit for a large, high-entropy PNG.
        with tempfile.TemporaryDirectory(prefix="yui-frame-", dir=temp_dir()) as folder:
            frame = Path(folder) / "frame.png"
            cmd = [get_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                   "-ss", f"{timestamp:.4f}", "-i", str(source), "-an", "-sn", "-dn", "-frames:v", "1",
                   "-vf", scale + ",format=rgba", "-map_metadata", "-1", "-update", "1", str(frame)]
            result = _run_capture(cmd, timeout=30, should_cancel=should_cancel, proc_holder=proc_holder)
            if not frame.exists() or not frame.stat().st_size:
                # Timestamp-free elementary streams need output-side seeking.
                pos = cmd.index("-ss")
                seek = cmd[pos:pos + 2]
                fallback = cmd[:pos] + cmd[pos + 2:]
                pos = fallback.index("-i") + 2
                fallback[pos:pos] = seek
                result = _run_capture(fallback, timeout=30, should_cancel=should_cancel, proc_holder=proc_holder)
            if result.returncode != 0 or not frame.exists() or not 0 < frame.stat().st_size <= MAX_STICKER_BYTES:
                raise MediaError("The preview frame could not be read. Choose another frame or source.")
            with _open_image(frame) as image:
                image.load()
                image = image.convert("RGBA")
    else:
        try:
            with _open_image(source) as source_image:
                if item.is_video:
                    frames = int(getattr(source_image, "n_frames", 1) or 1)
                    if frames > MAX_ANIMATION_FRAMES:
                        raise _error("This animation has too many frames. Trim it before designing.")
                    at = 0.0
                    for index in range(frames):
                        _cancel(should_cancel)
                        source_image.seek(index)
                        _check_canvas(*source_image.size)
                        at += max(20, int(source_image.info.get("duration", 100) or 100)) / 1000
                        if at > timestamp:
                            break
                source_image.load()
                image = ImageOps.exif_transpose(source_image).convert("RGBA")
        except (OSError, ValueError) as exc:
            raise _error("The preview image could not be read. Choose another image.") from exc
        image = image_transform(image, opts)
        if opts.fit == "fill":
            image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
        else:
            # FFmpeg's fit scale also enlarges small sources and tight crops.
            # contain mirrors that behaviour instead of leaving a tiny image
            # in the centre of an otherwise empty poster canvas.
            image = ImageOps.contain(image, (width, height), method=Image.Resampling.LANCZOS)
    _cancel(should_cancel)
    canvas = Image.new("RGBA", (width, height), ImageColor.getrgb("#" + _hex_color(opts.background)) + (255,))
    canvas.alpha_composite(image, ((width - image.width) // 2, (height - image.height) // 2))
    return canvas.convert("RGB")


def _protect_output(output: Path, protected_paths) -> None:
    if not is_local_path(output) or any(not is_local_path(Path(path)) for path in protected_paths):
        raise _error("Yui only reads and saves files on local drives.")
    try:
        for path in protected_paths:
            path = Path(path)
            if output.resolve() == path.resolve() or (output.exists() and path.exists() and output.samefile(path)):
                raise _error("The output cannot replace a source image, video, or sticker. Choose another filename.")
    except OSError as exc:
        raise _error("The destination could not be checked. Choose another filename or folder.") from exc


def export_poster(item, opts, layers, output, *, canvas_size, offset=0, max_side=1600,
                  overwrite=False, should_cancel=None, proc_holder=None, protected_paths=()) -> Path:
    """Export a metadata-free PNG of the current source frame and design."""
    from .engine import _publish_output
    _cancel(should_cancel)
    layers = validate_layers(layers)
    width, height = _canvas_size(canvas_size)
    if isinstance(max_side, bool) or not isinstance(max_side, int) or not 16 <= max_side <= MAX_OVERLAY_SIDE:
        raise _error("Choose a poster size between 16 and 4096 pixels.")
    output = Path(output)
    protected = (item.path, *(layer.image for layer in layers if layer.kind == "image"), *protected_paths)
    _protect_output(output, protected)
    if output.suffix.lower() != ".png":
        raise _error("Save the poster with a filename ending in .png.")
    ratio = max_side / max(width, height)
    size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
    _canvas_size(size)
    base = preview_frame(item, opts, size, offset=offset, should_cancel=should_cancel, proc_holder=proc_holder)
    overlay = compose_overlay(layers, size, should_cancel=should_cancel)
    base = base.convert("RGBA")
    base.alpha_composite(overlay)
    _cancel(should_cancel)
    try:
        with tempfile.TemporaryDirectory(prefix="yui-poster-", dir=temp_dir()) as folder:
            snapshot = Path(folder) / "poster.png"
            # A fresh raster carries no camera metadata, comments, account
            # names, source paths, or source PNG text chunks.
            clean = base.convert("RGB")
            clean.info.clear()
            clean.save(snapshot, format="PNG", compress_level=6)
            _cancel(should_cancel)
            _protect_output(output, protected)
            return _publish_output(snapshot, output, bool(overwrite), should_cancel or (lambda: False))
    except OSError as exc:
        raise _error("Could not save the poster. Check the destination folder and free space.") from exc
