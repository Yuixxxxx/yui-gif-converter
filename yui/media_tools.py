"""Local split and bounded, metadata-free PNG frame archives.

Extraction times and FPS refer to the selected source clip before GIF speed,
boomerang and palette reduction. Crop, colour effects and static designs are
included; reverse changes the archive's frame order.
"""

from __future__ import annotations

import math
import io
import tempfile
import time
import zipfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

from . import engine, overlays
from .paths import get_ffmpeg, is_local_path, temp_dir

MAX_FRAMES = 300
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MIN_PART_SECONDS = .1


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise engine.MediaError(f"{name} must be a number.")
    try:
        value = float(value)
    except (OverflowError, ValueError):
        raise engine.MediaError(f"{name} is outside the supported range.") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise engine.MediaError(f"{name} must be between {low:g} and {high:g}.")
    return value


def split_item(item: engine.MediaItem, at_seconds: float) -> tuple[engine.MediaItem, engine.MediaItem]:
    """Split a trimmed video/animation without altering its source file."""
    if not isinstance(item, engine.MediaItem) or not item.is_video:
        raise engine.MediaError("Choose a video or animation to split.")
    if not math.isfinite(item.duration) or item.duration < .2 or not math.isfinite(item.start) or not math.isfinite(item.end):
        raise engine.MediaError("This clip has no valid split range.")
    length = item.clip_seconds
    at = _number(at_seconds, "Split time", MIN_PART_SECONDS, max(MIN_PART_SECONDS, length - MIN_PART_SECONDS))
    if length < .2 or length - at < MIN_PART_SECONDS - 1e-9:
        raise engine.MediaError("Keep at least 0.1 seconds on each side of the split.")
    point = item.trim_start + at
    return (replace(item, start=item.trim_start, end=point),
            replace(item, start=point, end=item.trim_end))


def frame_range(item, *, start=0, end=None, fps=5) -> tuple[float, float, float, int]:
    """Validate the entire extraction request before opening input/output files."""
    if not isinstance(item, engine.MediaItem) or item.kind not in ("image", "video"):
        raise engine.MediaError("Choose an imported image, video or animation.")
    length = item.clip_seconds
    if not math.isfinite(length) or length > 86_400:
        raise engine.MediaError("This clip has no supported extraction range.")
    first = _number(start, "Start time", 0, length)
    last = length if end is None else _number(end, "End time", 0, length)
    rate = _number(fps, "Extraction FPS", .5, 30)
    if last <= first or last - first < .001:
        raise engine.MediaError("End time must be after start time.")
    count = max(1, math.ceil((last - first) * rate - 1e-9))
    if count > MAX_FRAMES:
        raise engine.MediaLimitError("Extract up to 300 frames at once. Shorten the range or lower FPS.")
    return first, last, rate, count


def frame_canvas(item, opts, max_side):
    from .transforms import crop_pixels
    if isinstance(max_side, bool) or not isinstance(max_side, int) or not 320 <= max_side <= 1920:
        raise engine.MediaError("Choose a frame size between 320 and 1920 pixels.")
    engine._check_canvas(item.width, item.height)
    left, top, right, bottom = crop_pixels((item.width, item.height), opts.crop)
    aspect = engine.ASPECTS.get(opts.aspect) or ((bottom - top) / (right - left))
    if aspect <= 1:
        return max_side, max(1, round(max_side * aspect))
    return max(1, round(max_side / aspect)), max_side


def _fresh_png(image, output):
    # Reconstruct the raster so EXIF, ICC, comments and PNG text cannot follow it.
    from PIL import Image
    clean = Image.frombytes("RGB", image.size, image.convert("RGB").tobytes())
    clean.save(output, format="PNG", compress_level=4)


def _fitted(image, opts, size):
    from PIL import Image, ImageColor, ImageOps
    from .transforms import image_transform
    image = image_transform(ImageOps.exif_transpose(image).convert("RGBA"), opts)
    if opts.fit == "fill":
        image = ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)
    else:
        image = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
    base = Image.new("RGBA", size, ImageColor.getrgb("#" + engine._hex_color(opts.background)) + (255,))
    base.alpha_composite(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
    return base.convert("RGB")


def extract_frames(item, opts, output, *, start=0, end=None, fps=5, max_side=640,
                   should_cancel=None, progress=None, proc_holder=None, protected_paths=()) -> Path:
    """Publish one PNG ZIP after a complete bounded extraction succeeds.

    Existing archives are preserved with a numbered filename. No source media,
    sticker, path, creator metadata or timestamps are written into the ZIP.
    """
    from PIL import Image
    from .transforms import ffmpeg_filters, validate_edit_options
    cancelled = should_cancel or (lambda: False)

    def check_cancel():
        if cancelled():
            raise engine.RenderCancelled()

    check_cancel()
    first, last, rate, count = frame_range(item, start=start, end=end, fps=fps)
    opts = validate_edit_options(replace(opts))
    size = frame_canvas(item, opts, max_side)
    layers = overlays.validate_layers(opts.overlays)
    source, output = Path(item.path), Path(output)
    protected = (source, *(layer.image for layer in layers if layer.kind == "image"), *protected_paths)
    if output.suffix.lower() != ".zip":
        raise engine.MediaError("Save extracted frames with a filename ending in .zip.")
    overlays._protect_output(output, protected)
    if not is_local_path(source) or not source.is_file():
        raise engine.MediaError("Choose source media on a local drive.")
    reverse = opts.reverse
    opts = replace(opts, reverse=False)

    def report(value, message):
        if progress is not None:
            progress(max(0.0, min(1.0, value)), message)

    report(0, "Preparing extracted frames…")
    try:
        with tempfile.TemporaryDirectory(prefix="yui-extract-", dir=temp_dir()) as folder:
            work = Path(folder)
            frames = work / "frames"
            frames.mkdir()
            frozen_layers = overlays.snapshot_layers(layers, work / "stickers", should_cancel=cancelled) if layers else ()
            overlay = overlays.compose_overlay(frozen_layers, size, should_cancel=cancelled) if layers else None

            def check_disk():
                if sum(path.stat().st_size for path in work.rglob("*") if path.is_file()) > MAX_ARCHIVE_BYTES:
                    raise engine.MediaLimitError("Extracted frames need too much space. Choose a smaller size or range.")

            check_disk()
            raw_paths = []
            total = 0
            if item.is_video and item.decoder == "ffmpeg":
                filters = ffmpeg_filters(opts, item.width, item.height)
                mode = "increase" if opts.fit == "fill" else "decrease"
                filters += [f"scale=w={size[0]}:h={size[1]}:force_original_aspect_ratio={mode}:flags=lanczos"]
                if opts.fit == "fill":
                    filters += [f"crop={size[0]}:{size[1]}"]
                else:
                    filters += [f"pad={size[0]}:{size[1]}:(ow-iw)/2:(oh-ih)/2:color=0x{engine._hex_color(opts.background)}"]
                filters += [f"fps=fps={rate:.8f}:start_time=0:round=up", "format=rgb24"]
                last_disk_check = [0.0]

                def bounded_cancel():
                    check_cancel()
                    now = time.monotonic()
                    if now - last_disk_check[0] >= .05:
                        last_disk_check[0] = now
                        paths = list(frames.glob("frame*.png"))
                        check_disk()
                        report(.65 * min(1, len(paths) / count), f"Reading frames {min(count, len(paths))}/{count}…")
                    return False

                cmd = [get_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                       "-ss", f"{item.trim_start + first:.8f}", "-i", str(source),
                       "-t", f"{last - first:.8f}", "-an", "-sn", "-dn", "-vf", ",".join(filters),
                       "-frames:v", str(count), "-map_metadata", "-1", "-threads", "1", str(frames / "frame%04d.png")]
                result = engine._run_capture(cmd, timeout=max(60, min(300, (last - first) * 2 + 30)),
                                             should_cancel=bounded_cancel, proc_holder=proc_holder)
                raw_paths = sorted(frames.glob("frame*.png"))
                if result.returncode != 0 or not raw_paths:
                    raise engine.MediaError("The selected frames could not be read. Choose another range or source.")
                # A tiny tail can fall after the final real frame. Repeating that
                # last decoded frame keeps source sampling deterministic.
                while len(raw_paths) < count:
                    raw_paths.append(raw_paths[-1])
                if len(raw_paths) > count:
                    raise engine.MediaError("The decoder returned too many frames.")
            else:
                with engine._open_image(source) as image:
                    frame_count = int(getattr(image, "n_frames", 1) or 1)
                    if frame_count > engine.MAX_ANIMATION_FRAMES:
                        raise engine.MediaLimitError("This animation has too many frames. Use a shorter source.")
                    index, frame_end = 0, max(20, int(image.info.get("duration", 100) or 100)) / 1000
                    fitted = None
                    for number in range(count):
                        check_cancel()
                        target = (item.trim_start if item.is_video else 0) + first + number / rate
                        moved = False
                        while item.is_video and frame_end <= target and index + 1 < frame_count:
                            image.seek(index + 1)
                            index += 1
                            frame_end += max(20, int(image.info.get("duration", 100) or 100)) / 1000
                            moved = True
                            check_cancel()
                        if fitted is None or moved:
                            engine._check_canvas(*image.size)
                            fitted = _fitted(image, opts, size)
                        path = frames / f"frame{number + 1:04d}.png"
                        _fresh_png(fitted, path)
                        total += path.stat().st_size
                        check_disk()
                        raw_paths.append(path)
                        report(.65 * (number + 1) / count, f"Reading frames {number + 1}/{count}…")
            check_cancel()
            if reverse:
                raw_paths.reverse()
            archive = work / "frames.zip"
            total = 0
            remaining = Counter(raw_paths)
            # A constant timestamp and fixed member names avoid leaking source
            # file names, local paths, user names or source modification times.
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
                for number, path in enumerate(raw_paths):
                    check_cancel()
                    with engine._open_image(path) as image:
                        base = image.convert("RGBA")
                        if overlay is not None:
                            base.alpha_composite(overlay)
                        clean = io.BytesIO()
                        _fresh_png(base, clean)
                    payload = clean.getvalue()
                    total += len(payload)
                    if total > MAX_ARCHIVE_BYTES:
                        raise engine.MediaLimitError("Extracted frames need too much space. Choose a smaller size or range.")
                    entry = zipfile.ZipInfo(f"frame{number + 1:04d}.png", date_time=(1980, 1, 1, 0, 0, 0))
                    entry.compress_type = zipfile.ZIP_STORED
                    entry.create_system = 0
                    bundle.writestr(entry, payload)
                    remaining[path] -= 1
                    if not remaining[path]:
                        path.unlink()
                    bundle.fp.flush()
                    check_disk()
                    report(.65 + .34 * (number + 1) / count, f"Saving frames {number + 1}/{count}…")
            check_cancel()
            overlays._protect_output(output, protected)
            published = engine._publish_output(archive, output, False, cancelled)
            report(1, "Frame archive saved.")
            return published
    except (engine.MediaError, engine.RenderCancelled):
        raise
    except (OSError, ValueError, OverflowError) as exc:
        raise engine.MediaError("Could not extract frames. Check the source, destination and free space.") from exc
