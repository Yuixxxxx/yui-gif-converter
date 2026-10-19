"""Probe media, plan the GIF, and render it so the file never exceeds the size cap.

Every render is measured after encoding. If it is over the cap it is re-encoded
smaller, and only a file that fits is ever moved to the output path, so the
result is always at or under the chosen size (10 MB at most).
"""

from __future__ import annotations

import io
from contextlib import contextmanager
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import warnings
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

from .paths import fallback_output_dir, get_ffmpeg, is_local_path, temp_dir

MIB = 1024 * 1024
HARD_CAP_MB = 10
HARD_CAP_BYTES = HARD_CAP_MB * MIB
SIZE_CHOICES_MB = (10, 8, 5, 3, 2, 1)

CREATE_NO_WINDOW = 0x08000000

VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".wmv",
    ".mpeg", ".mpg", ".mpe", ".flv", ".3gp", ".3g2", ".ts",
    ".mts", ".m2ts", ".ogv", ".ogg", ".gif", ".asf", ".f4v",
    ".mxf", ".vob", ".divx", ".xvid", ".qt", ".rm", ".rmvb",
}
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".pjpeg", ".bmp", ".dib",
    ".webp", ".tif", ".tiff", ".ico",
}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

TRANSITIONS = {
    "none": "None",
    "fade": "Crossfade",
    "fadeblack": "Fade through black",
    "fadewhite": "Fade through white",
    "slideleft": "Slide",
    "wipeleft": "Wipe",
    "circleopen": "Circle",
}
ASPECTS = {"1:1": 1.0, "16:9": 9 / 16, "9:16": 16 / 9, "4:3": 3 / 4, "3:4": 4 / 3, "4:5": 5 / 4}
DITHERS = {
    "bayer": "bayer:bayer_scale=4",
    "floyd": "floyd_steinberg",
    "sierra": "sierra2_4a",
    "none": "none",
}
# fps multiplier, preferred minimum long side, minimum fps, automatic long-side cap
PRIORITIES = {
    "auto": (1.0, 240, 6, 800),
    "smooth": (1.4, 180, 8, 640),
    "sharp": (0.7, 360, 4, 1080),
}
# Rough bytes per pixel for one frame of each kind; corrected after every pass.
K_VIDEO, K_STILL, K_TRANSITION = 0.30, 0.55, 0.35
MIN_LONG_SIDE = 48
MAX_ATTEMPTS = 10
# Large automatic filter pools spend time synchronising the small GIF canvas.
# Keep decoding automatic, but leave room for the UI while filtering each pass.
FILTER_THREADS = max(1, min(4, os.cpu_count() or 1))
# Reject tiny compressed files that ask a decoder to allocate huge canvases.
# This still accepts 8K video and typical high-resolution camera photographs.
MAX_INPUT_PIXELS = 64_000_000
MAX_INPUT_SIDE = 16_384
MAX_ANIMATION_FRAMES = 20_000
MAX_CAPTURE_BYTES = 8 * MIB
MAX_ERROR_BYTES = 128 * 1024
MAX_PREPARED_BYTES = 512 * MIB
MAX_REVERSE_BYTES = 256 * MIB
# Playlist demuxers can follow paths embedded inside an innocent-looking .mp4.
# Only actual media containers/elementary streams are accepted from users.
MEDIA_DEMUXERS = (
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,asf,flv,gif,mpeg,mpegts,"
    "ogg,rm,mxf,mpegvideo,m4v,h264,hevc,image2,image2pipe,png_pipe,jpeg_pipe,bmp_pipe,"
    "webp_pipe,tiff_pipe,ico"
)
IMAGE_FORMATS = ("GIF", "PNG", "JPEG", "WEBP", "BMP", "TIFF", "ICO")


class MediaError(Exception):
    pass


class MediaLimitError(MediaError):
    """A resource limit must not be retried as a different visual effect."""


class RenderCancelled(Exception):
    pass


# ── formatting helpers ──────────────────────────────────────────────────────


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        if seconds < 10 and abs(seconds - round(seconds)) > 0.05:
            return f"{seconds:.1f}s"
        return f"{int(round(seconds))}s"
    m, s = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_size(n: int) -> str:
    """Sizes in the same units Windows Explorer uses (1 MB = 1024 KB)."""
    mb = n / MIB
    if mb >= 1:
        return f"{mb:.2f} MB"
    return f"{n / 1024:.0f} KB"


def fmt_time(seconds: float) -> str:
    """Editable timestamp: 83.5 → 1:23.5"""
    seconds = max(0.0, seconds)
    m, s = divmod(seconds, 60)
    text = f"{s:04.1f}" if m else f"{s:.1f}"
    return f"{int(m)}:{text}" if m else text


def parse_time(text: str) -> Optional[float]:
    """Accepts 12, 12.5, 1:05, 1:05.25 or 0:01:05. Returns None when invalid."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if any(not math.isfinite(p) or p < 0 for p in parts) or len(parts) > 3:
        return None
    value = 0.0
    for p in parts:
        value = value * 60 + p
    return value if math.isfinite(value) else None


def short_name(name: str, limit: int = 38) -> str:
    if len(name) <= limit:
        return name
    keep = limit - 1
    return name[: keep - 12] + "…" + name[-12:]


# ── ffmpeg process helpers ──────────────────────────────────────────────────


def _no_window() -> dict:
    return {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}


def _local_inputs(cmd: list[str], trusted_root: Optional[Path] = None) -> list[str]:
    """Block network protocols and manifests, allowing our own safe frame list."""
    local = []
    for index, arg in enumerate(cmd):
        if arg == "-i":
            formats = MEDIA_DEMUXERS
            # Only Yui's generated concat list may use that demuxer. Its names
            # are relative and FFmpeg's safe mode rejects escapes/absolute paths.
            if trusted_root is not None and index + 1 < len(cmd):
                prefix = cmd[max(0, index - 4):index]
                source = Path(cmd[index + 1])
                if (prefix == ["-f", "concat", "-safe", "1"]
                        and is_local_path(source) and is_local_path(trusted_root)
                        and source.resolve().is_relative_to(trusted_root.resolve())):
                    formats += ",concat"
            local += ["-protocol_whitelist", "file,pipe", "-format_whitelist", formats]
        local.append(arg)
    return local


def _run_capture(
    cmd: list[str], binary: bool = False, timeout: float = 60,
    should_cancel: Optional[Callable[[], bool]] = None, proc_holder: Optional[dict] = None,
    *, _trusted_root: Optional[Path] = None, _on_stdout: Optional[Callable[[bytes], None]] = None,
) -> subprocess.CompletedProcess:
    from .windows import ProcessGuard, child_environment

    executable = Path(cmd[0]).name.lower()
    converter = executable == "ffmpeg" or (executable.startswith("ffmpeg") and executable.endswith(".exe"))
    cmd = _local_inputs(cmd, _trusted_root) if converter else list(cmd)
    cancelled = should_cancel or (lambda: False)
    if cancelled():
        raise RenderCancelled()
    environment = {"env": child_environment()} if converter else {}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, **environment, **_no_window())
    try:
        guard = ProcessGuard(proc) if converter else None
    except OSError as exc:
        proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()
        if proc_holder is not None:
            proc_holder["proc"] = None
        raise MediaError("Could not start a protected converter process. Restart Yui and try again.") from exc
    if proc_holder is not None:
        proc_holder["proc"] = proc
    stdout, stderr = bytearray(), bytearray()
    reader_errors = []

    def drain(pipe, target, limit, callback=None):
        try:
            while chunk := pipe.read1(64 * 1024):
                target.extend(chunk)
                if len(target) > limit:
                    del target[:-limit]
                if callback is not None:
                    callback(chunk)
        except Exception as exc:
            reader_errors.append(exc)

    readers = [threading.Thread(target=drain, args=(proc.stdout, stdout, MAX_CAPTURE_BYTES, _on_stdout), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, stderr, MAX_ERROR_BYTES), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    try:
        # Poll independently of stdout: a stuck decoder may emit no progress.
        while proc.poll() is None:
            if cancelled():
                raise RenderCancelled()
            if reader_errors:
                raise MediaError("Could not read the decoder response.")
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd, timeout)
            time.sleep(0.02)
        if cancelled():
            raise RenderCancelled()
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        proc.wait()
        for reader in readers:
            reader.join(timeout=2)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
        if guard is not None:
            guard.close()
        if proc_holder is not None:
            proc_holder["proc"] = None
    out, err = bytes(stdout), bytes(stderr)
    if not binary:
        out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _check_canvas(width: int, height: int) -> None:
    if width < 1 or height < 1 or max(width, height) > MAX_INPUT_SIDE or width * height > MAX_INPUT_PIXELS:
        raise MediaError("This media canvas is too large. Resize it before importing.")


@contextmanager
def _open_image(source):
    """Apply the same pre-allocation checks to every Pillow decoder entry."""
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source, formats=IMAGE_FORMATS) as image:
                _check_canvas(*image.size)
                yield image
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise MediaError("This media canvas is too large. Resize it before importing.") from exc


# ── media items ─────────────────────────────────────────────────────────────


@dataclass
class MediaItem:
    path: Path
    kind: str  # "video" or "image"
    width: int
    height: int
    duration: float = 0.0  # source length in seconds (videos)
    fps: float = 0.0
    start: float = 0.0  # trim in (videos)
    end: float = 0.0  # trim out (videos); 0 means "until the end"
    hold: float = 2.0  # seconds on screen (images)
    decoder: str = "ffmpeg"  # "ffmpeg", or "frames" for animations Pillow decodes (WebP/APNG)
    thumb: object = field(default=None, repr=False, compare=False)

    @property
    def is_video(self) -> bool:
        return self.kind == "video"

    @property
    def trim_end(self) -> float:
        if self.end > 0:
            return min(self.end, self.duration)
        return self.duration

    @property
    def trim_start(self) -> float:
        return max(0.0, min(self.start, max(0.0, self.trim_end - 0.1)))

    @property
    def clip_seconds(self) -> float:
        if not self.is_video:
            return max(0.1, self.hold)
        return max(0.1, self.trim_end - self.trim_start)

    @property
    def trimmed(self) -> bool:
        return self.is_video and (self.trim_start > 0.01 or self.trim_end < self.duration - 0.01)

    @property
    def summary(self) -> str:
        if self.is_video:
            kind = "Animation" if self.decoder == "frames" or self.path.suffix.lower() == ".gif" else "Video"
            return f"{kind} · {fmt_duration(self.duration)} · {self.width}×{self.height}"
        return f"Image · {self.width}×{self.height}"


def _probe_pil(path: Path, should_cancel=None) -> Optional[tuple[int, int, int, float]]:
    """(width, height, frame count, total seconds) via Pillow, or None if Pillow can't read it."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with _open_image(path) as im:
            w, h = im.size
            try:
                orientation = im.getexif().get(0x0112, 1)
            except Exception:
                orientation = 1
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            if path.suffix.lower() == ".gif":
                # Classifying a GIF needs two frames, not a scan to EOF. Its
                # timing is read by the cancellable FFmpeg probe below.
                if should_cancel is not None and should_cancel():
                    raise RenderCancelled()
                try:
                    im.seek(1)
                    _check_canvas(*im.size)
                except EOFError:
                    return w, h, 1, 0.0
                return w, h, 2, 0.0
            n = int(getattr(im, "n_frames", 1) or 1)
            if n > MAX_ANIMATION_FRAMES:
                raise MediaError("This animation has too many frames. Trim it before importing.")
            total = 0.0
            if n > 1:
                for i in range(n):
                    if should_cancel is not None and should_cancel():
                        raise RenderCancelled()
                    im.seek(i)
                    _check_canvas(*im.size)
                    total += max(20, int(im.info.get("duration", 100) or 100)) / 1000
            return w, h, n, total
    except (RenderCancelled, MediaError):
        raise
    except Exception:
        return None


def _measure_duration(path: Path, ffmpeg: str, **capture) -> float:
    """Read to the end when the container does not report a duration."""
    for copy in (True, False):
        cmd = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1",
            "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
        ]
        if copy:
            cmd += ["-c:v", "copy"]  # fast packet scan; no decoding or output file
        cmd += ["-f", "null", "-"]
        try:
            proc = _run_capture(cmd, timeout=120, **capture)
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"{path.name}: reading the video length took too long.") from exc
        if proc.returncode != 0:
            continue
        times = re.findall(r"(?m)^out_time_us=(\d+)\s*$", proc.stdout or "")
        duration = max((int(value) for value in times), default=0) / 1_000_000
        if duration > 0:
            return duration
        # Raw streams may have no packet timestamps; decode to recover their timing.
    raise MediaError(f"{path.name}: the video length could not be read. Try re-saving the video.")


def _probe_ffmpeg(path: Path, ffmpeg: str, **capture) -> MediaItem:
    proc = _run_capture([ffmpeg, "-hide_banner", "-nostdin", "-i", str(path)], **capture)
    text = (proc.stderr or "") + (proc.stdout or "")
    if not text.strip():
        raise MediaError(f"{path.name}: could not read this file.")

    duration = 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    stream = next((line for line in text.splitlines() if "Video:" in line), "")
    if not stream:
        raise MediaError(f"{path.name}: no video found in this file.")
    size = re.search(r"(\d{2,5})x(\d{2,5})", stream)
    if not size:
        raise MediaError(f"{path.name}: could not read the video size.")
    width, height = int(size.group(1)), int(size.group(2))
    _check_canvas(width, height)

    fps = 30.0
    fps_m = re.search(r"(\d+(?:\.\d+)?)\s*fps", stream) or re.search(r"(\d+(?:\.\d+)?)\s*tbr", stream)
    if fps_m:
        fps = float(fps_m.group(1))
        if fps <= 0 or fps > 240:
            fps = 30.0

    rot = 0.0
    rm = re.search(r"rotate\s*:\s*(-?\d+(?:\.\d+)?)", text) or re.search(
        r"rotation of\s*(-?\d+(?:\.\d+)?)", text, re.I
    )
    if rm:
        rot = abs(float(rm.group(1)))
    if int(round(rot)) % 180 == 90:
        width, height = height, width

    if duration <= 0:
        duration = _measure_duration(path, ffmpeg, **capture)
    return MediaItem(path=path, kind="video", width=width, height=height, duration=duration, fps=fps)


def probe(path: Path, ffmpeg: Optional[str] = None, hold: float = 2.0, *,
          should_cancel=None, proc_holder=None) -> MediaItem:
    if should_cancel is not None and should_cancel():
        raise RenderCancelled()
    path = Path(path)
    if not is_local_path(path):
        raise MediaError("Yui only opens media on local drives; network shares are disabled.")
    if not path.is_file():
        raise MediaError(f"{path.name}: file not found.")
    ext = path.suffix.lower()
    if ext not in MEDIA_EXTS:
        raise MediaError(f"{path.name}: not a supported image or video.")

    if ext in IMAGE_EXTS or ext == ".gif":
        info = _probe_pil(path, should_cancel)
        if info:
            w, h, frames, total = info
            if frames <= 1:
                return MediaItem(path=path, kind="image", width=w, height=h, hold=hold)
            if ext in (".webp", ".png"):
                # FFmpeg can't play animated WebP, so Pillow decodes those frames.
                total = total or frames * 0.1
                return MediaItem(
                    path=path, kind="video", width=w, height=h, duration=total,
                    fps=min(50.0, frames / max(0.05, total)), decoder="frames",
                )
        elif ext != ".gif":
            raise MediaError(f"{path.name}: this image could not be opened.")
    return _probe_ffmpeg(path, ffmpeg or get_ffmpeg(), should_cancel=should_cancel, proc_holder=proc_holder)


def make_thumbnail(item: MediaItem, box: tuple[int, int] = (176, 100), ffmpeg: Optional[str] = None, *,
                   should_cancel=None, proc_holder=None):
    """Small preview image (PIL) for the media list."""
    from PIL import Image, ImageOps

    if should_cancel is not None and should_cancel():
        raise RenderCancelled()
    if not is_local_path(item.path):
        raise MediaError("Yui only opens media on local drives.")
    if item.is_video and item.decoder == "ffmpeg":
        t = min(item.trim_start + 0.5, item.trim_start + item.clip_seconds * 0.25)
        cmd = [
            ffmpeg or get_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error",
            "-ss", f"{t:.3f}", "-i", str(item.path), "-frames:v", "1",
            "-vf", f"scale={box[0] * 2}:{box[1] * 2}:force_original_aspect_ratio=decrease",
            "-f", "image2pipe", "-c:v", "png", "pipe:1",
        ]
        proc = _run_capture(cmd, binary=True, timeout=30, should_cancel=should_cancel, proc_holder=proc_holder)
        if not proc.stdout:
            # Timestamp-free streams cannot seek in their container. Decode the
            # short leading section instead, while retaining fast seeking normally.
            seek = cmd.index("-ss")
            position = cmd[seek:seek + 2]
            cmd = cmd[:seek] + cmd[seek + 2:]
            after_input = cmd.index("-i") + 2
            cmd = cmd[:after_input] + position + cmd[after_input:]
            proc = _run_capture(cmd, binary=True, timeout=30,
                                should_cancel=should_cancel, proc_holder=proc_holder)
        if not proc.stdout:
            raise MediaError("No preview frame")
        with _open_image(io.BytesIO(proc.stdout)) as src:
            src.load()
            im = src.copy()
    else:
        with _open_image(item.path) as src:
            im = ImageOps.exif_transpose(src)
            im.load()
    im = im.convert("RGBA")
    im.thumbnail((box[0] * 2, box[1] * 2))
    return im


# ── options & planning ──────────────────────────────────────────────────────


@dataclass
class RenderOptions:
    max_mb: float = 10
    priority: str = "auto"  # auto | smooth | sharp
    fps: int = 0  # 0 = automatic
    max_side: int = 0  # longest side cap in px, 0 = automatic
    dither: str = "auto"  # auto | bayer | floyd | sierra | none
    loop: int = 0  # 0 forever, -1 play once, n = repeat n more times
    speed: float = 1.0
    boomerang: bool = False
    aspect: str = "auto"  # auto (first item) or a key of ASPECTS
    fit: str = "fit"  # fit (letterbox) | fill (crop)
    background: str = "#000000"
    transition: str = "none"
    transition_seconds: float = 0.5
    overlays: tuple = ()  # Session-only text/image layers, composited above every frame.
    crop: tuple | None = None  # Normalised left, top, width, height inside each source.
    effect: str = "none"
    reverse: bool = False  # Reverse the joined sequence, independently of boomerang.
    palette_colors: int = 256
    palette_mode: str = "auto"  # auto | full

    @property
    def cap_bytes(self) -> int:
        mb = min(float(HARD_CAP_MB), max(0.5, float(self.max_mb or HARD_CAP_MB)))
        return int(mb * MIB)


@dataclass
class Plan:
    width: int
    height: int
    delay: int  # centiseconds per frame; GIF timing is stored this way
    segments: tuple[float, ...]
    transition: str = "none"
    transition_seconds: float = 0.0
    boomerang: bool = False

    @property
    def fps(self) -> float:
        return 100 / self.delay

    @property
    def fps_expr(self) -> str:
        return f"100/{self.delay}"

    @property
    def total_seconds(self) -> float:
        n = len(self.segments)
        overlap = self.transition_seconds * (n - 1) if self.transition != "none" and n > 1 else 0.0
        total = sum(self.segments) - overlap
        return total * (2 if self.boomerang else 1)


def _even(n: float) -> int:
    n = int(n)
    return max(2, n - (n % 2))


def _base_fps(seconds: float) -> float:
    for limit, fps in ((3, 20), (6, 15), (10, 12), (20, 10), (40, 8), (90, 6)):
        if seconds <= limit:
            return fps
    return 5


class Planner:
    """Chooses canvas size and frame rate, and re-solves them from measured sizes."""

    def __init__(self, items: list[MediaItem], opts: RenderOptions) -> None:
        from .transforms import validate_edit_options, crop_pixels
        validate_edit_options(opts)
        if not items:
            raise MediaError("Add at least one image or video.")
        self.items = items
        self.opts = opts
        speed = min(8.0, max(0.1, float(opts.speed or 1.0)))
        self.segments = tuple(it.clip_seconds / speed if it.is_video else it.clip_seconds for it in items)
        n = len(items)

        self.transition = opts.transition if n > 1 and opts.transition in TRANSITIONS else "none"
        self.transition_seconds = 0.0
        if self.transition != "none":
            self.transition_seconds = min(float(opts.transition_seconds), 0.45 * min(self.segments))
            if self.transition_seconds < 0.1:
                self.transition, self.transition_seconds = "none", 0.0
        self.boomerang = bool(opts.boomerang)

        fps_mult, self.floor_long, self.min_fps, auto_cap = PRIORITIES.get(opts.priority, PRIORITIES["auto"])
        self.has_video = any(it.is_video for it in items)
        video_seconds = sum(s for it, s in zip(items, self.segments) if it.is_video)
        if opts.fps and opts.fps > 0:
            target = float(opts.fps)
        elif self.has_video:
            target = _base_fps(video_seconds * (2 if self.boomerang else 1)) * fps_mult
            src = max((it.fps for it in items if it.is_video), default=0.0)
            if src > 0 and self.transition == "none":
                target = min(target, max(4.0, src))
        elif self.transition != "none":
            target = 15.0
        else:
            target = 4.0  # stills only: frames only need to mark time
        self.target_delay = int(min(50, max(2, round(100 / target))))

        first = items[0]
        boxes = [crop_pixels((it.width, it.height), opts.crop) for it in items]
        first_box = boxes[0]
        self.aspect = ASPECTS.get(opts.aspect) or ((first_box[3] - first_box[1]) / max(1, first_box[2] - first_box[0]))
        self.ratio = min(self.aspect, 1 / self.aspect)  # area = long² × ratio
        natural = max(max(box[2] - box[0], box[3] - box[1]) for box in boxes)
        cap = opts.max_side if opts.max_side and opts.max_side > 0 else auto_cap
        self.cap_long = max(MIN_LONG_SIDE, min(int(cap), natural))

    def weighted_frames(self, fps: float) -> float:
        video = sum(s * fps for it, s in zip(self.items, self.segments) if it.is_video)
        stills = sum(1 for it in self.items if not it.is_video)
        trans = self.transition_seconds * fps * (len(self.items) - 1) if self.transition != "none" else 0.0
        total = K_VIDEO * video + K_STILL * stills + K_TRANSITION * trans
        return max(0.2, total * (2 if self.boomerang else 1))

    def predict(self, delay: int, long_side: int) -> float:
        return long_side * long_side * self.ratio * self.weighted_frames(100 / delay)

    def fit(self, scale: float, aim_bytes: float) -> tuple[int, int]:
        """Best (delay, long side) expected to land on aim_bytes given a measured scale factor."""

        def long_for(delay: int) -> float:
            per = self.ratio * self.weighted_frames(100 / delay) * scale
            return math.sqrt(aim_bytes / per) if per > 0 else float(self.cap_long)

        delay = self.target_delay
        long_side = long_for(delay)
        if self.has_video or self.transition != "none":
            # Trade frame rate for resolution until the preferred floor is reached.
            while long_side < self.floor_long and 100 / (delay + 1) >= self.min_fps and delay < 50:
                delay += 1
                long_side = long_for(delay)
            while long_side < 96 and delay < 34:
                delay += 1
                long_side = long_for(delay)
        long_side = min(long_side, self.cap_long)
        return delay, max(MIN_LONG_SIDE, int(long_side))

    def dims(self, long_side: int) -> tuple[int, int]:
        if self.aspect <= 1:
            w, h = long_side, long_side * self.aspect
        else:
            w, h = long_side / self.aspect, long_side
        return max(16, _even(w)), max(16, _even(h))

    def plan(self, delay: int, long_side: int) -> Plan:
        w, h = self.dims(long_side)
        return Plan(
            width=w, height=h, delay=delay, segments=self.segments,
            transition=self.transition, transition_seconds=self.transition_seconds,
            boomerang=self.boomerang,
        )

    def initial(self) -> Plan:
        delay, long_side = self.fit(1.0, self.opts.cap_bytes * 0.94)
        return self.plan(delay, long_side)


def estimate(items: list[MediaItem], opts: RenderOptions) -> Plan:
    return Planner(items, opts).initial()


# ── rendering ───────────────────────────────────────────────────────────────


@dataclass
class RenderResult:
    output: Path
    size: int
    width: int
    height: int
    fps: float
    seconds: float
    passes: int
    cap_bytes: int


def _hex_color(value: str) -> str:
    value = (value or "#000000").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        value = "000000"
    return value.lower()


class _Prepared:
    """Per-item ffmpeg input arguments, built once per render."""

    def __init__(self, item: MediaItem, args_for_fps: Callable[[str], list[str]], is_video: bool,
                 *, cropped: bool = False) -> None:
        self.item = item
        self.args_for_fps = args_for_fps
        self.is_video = is_video
        self.cropped = cropped


def _prepare_items(
    items: list[MediaItem], workdir: Path, opts: RenderOptions,
    should_cancel: Callable[[], bool] = lambda: False,
) -> list[_Prepared]:
    from PIL import Image, ImageOps
    from .transforms import crop_pixels

    prepared: list[_Prepared] = []
    prepared_bytes = 0
    for index, item in enumerate(items):
        if should_cancel():
            raise RenderCancelled()
        if not item.is_video:
            # Pillow applies the camera rotation and keeps transparency; the PNG is then fed to ffmpeg.
            png = workdir / f"still_{index:03d}.png"
            with _open_image(item.path) as src:
                im = ImageOps.exif_transpose(src)
                im.load()
            im = im.convert("RGBA" if ("A" in im.getbands() or "transparency" in im.info) else "RGB")
            if opts.crop is not None:
                im = im.crop(crop_pixels(im.size, opts.crop))
            im.thumbnail((1600, 1600), Image.LANCZOS)
            im.save(png, format="PNG", compress_level=1)
            prepared_bytes += png.stat().st_size
            hold = item.clip_seconds

            def still_args(fps: str, png=png, hold=hold) -> list[str]:
                return ["-loop", "1", "-framerate", fps, "-t", f"{hold + 0.2:.3f}", "-i", str(png)]

            prepared.append(_Prepared(item, still_args, False, cropped=opts.crop is not None))
        elif item.decoder == "frames":
            listing = _explode_animation(item, workdir / f"anim_{index:03d}", should_cancel,
                                         max_bytes=MAX_PREPARED_BYTES - prepared_bytes, crop=opts.crop)
            prepared_bytes += sum(path.stat().st_size for path in listing.parent.iterdir() if path.is_file())

            def frame_args(fps: str, listing=listing) -> list[str]:
                return ["-f", "concat", "-safe", "1", "-i", str(listing)]

            prepared.append(_Prepared(item, frame_args, True, cropped=opts.crop is not None))
        else:
            start, length = item.trim_start, item.clip_seconds

            def video_args(fps: str, item=item, start=start, length=length) -> list[str]:
                args = ["-ss", f"{start:.3f}"] if start > 0.001 else []
                return args + ["-t", f"{length:.3f}", "-i", str(item.path)]

            prepared.append(_Prepared(item, video_args, True))
        if prepared_bytes > MAX_PREPARED_BYTES:
            raise MediaLimitError("These images need too much temporary space. Trim animations or use fewer images.")
    return prepared


def _explode_animation(
    item: MediaItem, folder: Path, should_cancel: Callable[[], bool] = lambda: False,
    *, max_bytes: int = MAX_PREPARED_BYTES, crop: tuple | None = None,
) -> Path:
    """Write the frames of an animated WebP/APNG inside the trim range plus a concat list."""
    from PIL import Image, ImageOps
    from .transforms import crop_pixels

    folder.mkdir(parents=True, exist_ok=True)
    lines = ["ffconcat version 1.0"]
    start, end = item.trim_start, item.trim_end
    t = 0.0
    last = None
    written = 0
    with _open_image(item.path) as im:
        n = int(getattr(im, "n_frames", 1) or 1)
        if n > MAX_ANIMATION_FRAMES:
            raise MediaError("This animation has too many frames. Trim it before importing.")
        for i in range(n):
            if should_cancel():
                raise RenderCancelled()
            im.seek(i)
            _check_canvas(*im.size)
            dur = max(20, int(im.info.get("duration", 100) or 100)) / 1000
            f_start, f_end = t, t + dur
            t = f_end
            if f_end <= start or f_start >= end:
                continue
            shown = min(f_end, end) - max(f_start, start)
            frame = im.convert("RGBA")
            if crop is not None:
                frame = ImageOps.exif_transpose(frame)
                frame = frame.crop(crop_pixels(frame.size, crop))
            frame.thumbnail((1280, 1280))
            name = folder / f"f{i:05d}.png"
            frame.save(name, format="PNG", compress_level=1)
            written += name.stat().st_size
            if written > max_bytes:
                raise MediaLimitError("This animation needs too much temporary space. Trim it before converting.")
            lines.append(f"file '{name.name}'")
            lines.append(f"duration {shown:.4f}")
            last = name
    if last is None:
        raise MediaError(f"{item.path.name}: nothing left after trimming.")
    lines.append(f"file '{last.name}'")
    listing = folder / "frames.ffconcat"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return listing


def _build_graph(prepared: list[_Prepared], plan: Plan, opts: RenderOptions, tail: str,
                 *, overlay_index: Optional[int] = None) -> str:
    """Filter graph that normalises each item onto one canvas, joins them and ends with `tail`."""
    w, h, fps = plan.width, plan.height, plan.fps_expr
    from .transforms import ffmpeg_filters
    if plan.boomerang or opts.reverse:
        reverse_frames = max(1, math.ceil(plan.total_seconds * plan.fps / (2 if plan.boomerang else 1)))
        # Two reverse stages can retain their source frames simultaneously.
        buffers = 2 if plan.boomerang and opts.reverse else 1
        if w * h * reverse_frames * 4 * buffers > MAX_REVERSE_BYTES:
            raise MediaLimitError("This reverse or boomerang needs too much memory. Trim it or lower the frame rate or resolution.")
    bg = _hex_color(opts.background)
    speed = min(8.0, max(0.1, float(opts.speed or 1.0)))
    parts: list[str] = []
    for i, (prep, seconds) in enumerate(zip(prepared, plan.segments)):
        chain = []
        if prep.is_video and abs(speed - 1.0) > 1e-3:
            chain.append(f"setpts=(PTS-STARTPTS)/{speed:.4f}")
        else:
            chain.append("setpts=PTS-STARTPTS")
        chain.append(f"fps={fps}")
        transform_opts = replace(opts, crop=None) if prep.cropped else opts
        chain.extend(ffmpeg_filters(transform_opts, prep.item.width, prep.item.height))
        if opts.fit == "fill":
            chain.append(f"scale=w={w}:h={h}:force_original_aspect_ratio=increase:flags=lanczos")
            chain.append(f"crop={w}:{h}")
        else:
            chain.append(f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:flags=lanczos")
        chain.append("format=rgba")
        parts.append(f"[{i}:v]{','.join(chain)}[f{i}]")
        parts.append(f"color=c=0x{bg}:s={w}x{h}:r={fps}[b{i}]")
        parts.append(
            f"[b{i}][f{i}]overlay=x=(W-w)/2:y=(H-h)/2:shortest=1,setsar=1,format=gbrp,"
            f"tpad=stop_mode=clone:stop_duration={seconds:.3f},"
            f"trim=duration={seconds:.3f},setpts=PTS-STARTPTS[v{i}]"
        )

    n = len(prepared)
    last = "v0"
    if n > 1 and plan.transition != "none":
        length = plan.segments[0]
        for i in range(1, n):
            offset = max(0.0, length - plan.transition_seconds)
            out = f"x{i}"
            parts.append(
                f"[{last}][v{i}]xfade=transition={plan.transition}:"
                f"duration={plan.transition_seconds:.3f}:offset={offset:.3f}[{out}]"
            )
            length = length + plan.segments[i] - plan.transition_seconds
            last = out
    elif n > 1:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[cat]")
        last = "cat"
    if opts.reverse:
        parts.append(f"[{last}]reverse,setpts=PTS-STARTPTS[reversed]")
        last = "reversed"
    if plan.boomerang:
        parts.append(f"[{last}]split[bf][bb];[bb]reverse[br];[bf][br]concat=n=2:v=1:a=0[bm]")
        last = "bm"
    if overlay_index is not None:
        # The stationary design covers the complete joined timeline, including
        # transitions and the reverse half of a boomerang. Add it before
        # palette analysis and quantisation so its colours reach the GIF.
        parts.append(f"[{last}][{overlay_index}:v]overlay=x=0:y=0:shortest=1:format=auto,"
                     f"trim=duration={plan.total_seconds:.4f},format=gbrp[decorated]")
        last = "decorated"
    parts.append(tail.replace("{in}", f"[{last}]"))
    return ";".join(parts)


class _Runner:
    MIN_TIMEOUT = 90.0
    MAX_TIMEOUT = 900.0
    def __init__(
        self,
        ffmpeg: str,
        workdir: Path,
        progress: Callable[[float, str], None],
        should_cancel: Callable[[], bool],
        proc_holder: Optional[dict],
    ) -> None:
        self.ffmpeg = ffmpeg
        self.workdir = workdir
        self.progress = progress
        self.should_cancel = should_cancel
        self.proc_holder = proc_holder
        self._graph_count = 0

    def graph_args(self, graph: str) -> list[str]:
        if len(graph) < 6000:
            return ["-filter_complex", graph]
        # Long graphs (many items) go through a file to stay under the Windows command-line limit.
        self._graph_count += 1
        path = self.workdir / f"graph_{self._graph_count}.txt"
        path.write_text(graph, encoding="utf-8")
        return ["-/filter_complex", str(path)]

    def run(self, args: list[str], total_seconds: float, lo: float, hi: float, message: str) -> None:
        if self.should_cancel():
            raise RenderCancelled()
        cmd = [
            self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats", "-filter_complex_threads", str(FILTER_THREADS),
        ] + args
        self.progress(lo, message)
        pending = bytearray()

        def on_stdout(chunk):
            pending.extend(chunk)
            while b"\n" in pending:
                raw, _, rest = pending.partition(b"\n")
                pending[:] = rest
                line = raw.decode("ascii", "replace").strip()
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if us > 0 and total_seconds > 0:
                        frac = min(1.0, us / (total_seconds * 1_000_000))
                        self.progress(lo + (hi - lo) * frac, message)
            # An invalid child response cannot grow an unterminated line forever.
            if len(pending) > 4096:
                pending.clear()

        timeout = min(self.MAX_TIMEOUT, max(self.MIN_TIMEOUT, total_seconds * 12 + 30))
        try:
            result = _run_capture(cmd, timeout=timeout, should_cancel=self.should_cancel,
                                  proc_holder=self.proc_holder, _trusted_root=self.workdir,
                                  _on_stdout=on_stdout)
        except subprocess.TimeoutExpired as exc:
            raise MediaError("Processing took too long. Trim the clip or lower the resolution and try again.") from exc
        if self.should_cancel():
            raise RenderCancelled()
        if result.returncode != 0:
            # Decoder diagnostics may contain private paths or embedded strings.
            raise MediaError("The media could not be processed. Try another file or re-save it first.")


def render(
    items: list[MediaItem],
    opts: RenderOptions,
    output: Path,
    progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    proc_holder: Optional[dict] = None,
    *, overwrite: bool = False,
) -> RenderResult:
    """Render items (one or many, images and/or videos) into a single GIF at or under the cap."""
    output = Path(output)
    from .overlays import validate_layers, compose_overlay, snapshot_layers, _protect_output
    layers = validate_layers(opts.overlays)
    if not is_local_path(output) or any(not is_local_path(item.path) for item in items):
        raise MediaError("Yui only reads and saves files on local drives.")
    if layers:
        _protect_output(output, ( *(item.path for item in items),
                                  *(layer.image for layer in layers if layer.kind == "image")))
    if any(output.resolve() == item.path.resolve() for item in items):
        raise MediaError("The output cannot replace an input file. Choose another name or folder.")
    cap = opts.cap_bytes
    cap_label = f"{cap / MIB:g} MB"
    emit = progress or (lambda f, m: None)
    cancelled = should_cancel or (lambda: False)
    ffmpeg = get_ffmpeg()
    planner = Planner(items, opts)
    workdir = Path(tempfile.mkdtemp(prefix="yui-", dir=temp_dir()))
    runner = _Runner(ffmpeg, workdir, lambda f, m: emit(max(0.0, min(1.0, f)), m), cancelled, proc_holder)
    has_stills = any(not it.is_video for it in items)
    # The bundled converter's moving-area statistics omit complete scene
    # changes. Full-scene statistics preserve colours throughout the clip.
    stats_mode = "full"
    if opts.dither in DITHERS:
        dither = DITHERS[opts.dither]
    else:
        dither = "sierra2_4a" if not planner.has_video else "bayer:bayer_scale=5"

    try:
        emit(0.01, "Preparing files…")
        layers = snapshot_layers(layers, workdir / "design", cancelled) if layers else ()
        prepared = _prepare_items(items, workdir, opts, cancelled)
        palettes: dict[int, Path] = {}
        overlay_files: dict[tuple[int, int], Path] = {}
        overlay_index = len(prepared) if layers else None

        def overlay_for(plan: Plan) -> Path:
            key = (plan.width, plan.height)
            if key not in overlay_files:
                path = workdir / f"design_{plan.width}x{plan.height}.png"
                image = compose_overlay(layers, key, should_cancel=cancelled)
                image.info.clear()
                image.save(path, format="PNG", compress_level=1)
                overlay_files[key] = path
            return overlay_files[key]

        def inputs_for(plan: Plan) -> list[str]:
            args: list[str] = []
            for prep in prepared:
                args += prep.args_for_fps(plan.fps_expr)
            if layers:
                args += ["-loop", "1", "-framerate", plan.fps_expr, "-i", str(overlay_for(plan))]
            return args

        def palette_for(colors: int, plan: Plan) -> Path:
            if colors in palettes:
                return palettes[colors]
            # Colours barely depend on size, so analyse a small, low-frame-rate copy once.
            sample = planner.plan(max(plan.delay, 10), min(480, max(plan.width, plan.height)))
            path = workdir / f"palette_{colors}.png"
            tail = f"{{in}}palettegen=max_colors={colors}:stats_mode={stats_mode}[out]"
            # Reversal changes ordering, not the colours present. Analyse in
            # source order to avoid buffering the timeline a second time.
            palette_opts = replace(opts, reverse=False) if opts.reverse else opts
            graph = _build_graph(prepared, sample, palette_opts, tail, overlay_index=overlay_index)
            runner.run(
                inputs_for(sample) + runner.graph_args(graph)
                + ["-map", "[out]", "-frames:v", "1", "-update", "1", str(path)],
                sample.total_seconds, 0.02, 0.12, "Analyzing colors…",
            )
            palettes[colors] = path
            return path

        scale = 1.0  # measured bytes ÷ predicted bytes, refined after every pass
        squeeze = 1.0  # extra shrink when the model stops moving
        colors = opts.palette_colors
        best: Optional[tuple[int, Path, Plan]] = None
        grown = grow_pending = False
        fails = passes = 0
        prev_key: Optional[tuple[int, int]] = None

        for attempt in range(MAX_ATTEMPTS):
            if cancelled():
                raise RenderCancelled()
            aim = cap * (0.90 if grow_pending else max(0.78, 0.94 - 0.04 * fails))
            delay, long_side = planner.fit(scale, aim)
            if (delay, long_side) == prev_key and not grow_pending:
                squeeze *= 0.88
            prev_key = (delay, long_side)
            grow_pending = False
            long_side = max(MIN_LONG_SIDE, int(long_side * squeeze))
            plan = planner.plan(delay, long_side)

            tmp = workdir / f"pass_{attempt}.gif"
            palette_index = len(prepared) + (1 if layers else 0)
            tail = f"{{in}}[{palette_index}:v]paletteuse=dither={dither}:diff_mode=rectangle[out]"
            label = "Encoding GIF…" if attempt == 0 else f"Pass {attempt + 1} · fitting under {cap_label}…"
            try:
                palette = palette_for(colors, plan)
                graph = _build_graph(prepared, plan, opts, tail, overlay_index=overlay_index)
                runner.run(
                    inputs_for(plan) + ["-i", str(palette)] + runner.graph_args(graph)
                    + ["-map", "[out]", "-map_metadata", "-1", "-map_chapters", "-1",
                       "-loop", str(int(opts.loop)), "-f", "gif", str(tmp)],
                    plan.total_seconds, 0.12, 0.98, label,
                )
            except MediaLimitError:
                raise
            except MediaError:
                # Drop the optional effects first; they are the most likely to upset ffmpeg.
                if planner.transition != "none":
                    planner.transition, planner.transition_seconds = "none", 0.0
                    palettes.clear()
                    continue
                if planner.boomerang:
                    planner.boomerang = False
                    palettes.clear()
                    continue
                if best is not None:
                    break
                if attempt >= MAX_ATTEMPTS - 1:
                    raise
                scale *= 1.6
                fails += 1
                continue
            passes += 1

            size = tmp.stat().st_size if tmp.exists() else 0
            if size <= 0:
                fails += 1
                scale *= 1.4
                continue
            scale = max(1e-6, size / max(1.0, planner.predict(plan.delay, max(plan.width, plan.height))))

            if size <= cap:
                if best is None or size > best[0]:
                    best = (size, tmp, plan)
                if not grown and size < cap * 0.62:
                    # Lots of room left: try one sharper pass and keep whichever fits best.
                    nd, nl = planner.fit(scale, cap * 0.90)
                    if nl * squeeze >= long_side * 1.1 or nd < delay:
                        grown = grow_pending = True
                        continue
                break
            if best is not None:
                break  # a quality-up pass overshot; keep the earlier file that fits
            fails += 1
            if fails >= 3:
                dither = "none"
            if fails >= 4:
                colors = min(colors, 128)
            if fails >= 6:
                colors = min(colors, 64)

        if best is None:
            raise MediaError(
                f"This is too long to fit in a GIF under {cap_label}, even at the lowest quality. "
                "Trim it, raise the speed, or remove some items."
            )

        size, tmp, plan = best
        if size > cap or size > HARD_CAP_BYTES:  # defensive: never hand back an oversized file
            raise MediaError(f"Could not get the GIF under {cap_label}.")
        if cancelled():
            raise RenderCancelled()
        output = _publish_output(tmp, output, overwrite, cancelled)
        emit(1.0, "Done")
        return RenderResult(
            output=output, size=size, width=plan.width, height=plan.height,
            fps=plan.fps, seconds=plan.total_seconds, passes=passes, cap_bytes=cap,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── output naming ───────────────────────────────────────────────────────────


def save_copy(
    source: Path, output: Path, *, overwrite: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Path:
    """Save an unchanged copy of a completed GIF to another local location."""
    import warnings
    from PIL import Image

    source, output = Path(source), Path(output)
    # Do this before stat, resolve, samefile or open: network access can trigger
    # Windows authentication even when no file is ultimately read or written.
    if not is_local_path(source) or not is_local_path(output):
        raise MediaError("Yui only reads and saves files on local drives.")
    if output.suffix.lower() != ".gif":
        raise MediaError("Choose a filename ending in .gif.")
    cancelled = should_cancel or (lambda: False)
    if cancelled():
        raise RenderCancelled()
    try:
        if source.resolve() == output.resolve() or (output.exists() and source.samefile(output)):
            raise MediaError("Save a copy with another name or in another folder.")
        if not source.is_file():
            raise MediaError("The completed GIF is unavailable. Create it again before saving a copy.")
        if not 0 < source.stat().st_size <= HARD_CAP_BYTES:
            raise MediaError("Only GIFs up to 10 MB can be saved with Yui.")
        # Validate exactly the bytes we publish, even if the original changes.
        chunks = []
        size = 0
        with source.open("rb") as fh:
            while chunk := fh.read(min(64 * 1024, HARD_CAP_BYTES + 1 - size)):
                if cancelled():
                    raise RenderCancelled()
                chunks.append(chunk)
                size += len(chunk)
                if size > HARD_CAP_BYTES:
                    raise MediaError("Only GIFs up to 10 MB can be saved with Yui.")
        payload = b"".join(chunks)
        if not payload:
            raise MediaError("The completed GIF is empty. Create it again before saving a copy.")
    except OSError as exc:
        raise MediaError("Could not read the completed GIF. Create it again or check whether the file is available.") from exc
    try:
        with _open_image(io.BytesIO(payload)) as image:
            if image.format != "GIF":
                raise MediaError("The completed file is not a GIF. Create it again before saving a copy.")
            count = 0
            while True:
                if cancelled():
                    raise RenderCancelled()
                count += 1
                if count > MAX_ANIMATION_FRAMES:
                    raise MediaError("This animation has too many frames. Trim it before saving a copy.")
                _check_canvas(*image.size)
                image.load()
                try:
                    image.seek(image.tell() + 1)
                except EOFError:
                    break
    except (MediaError, RenderCancelled):
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaError("The completed GIF is damaged or cannot be read. Create it again before saving a copy.") from exc
    if cancelled():
        raise RenderCancelled()
    try:
        with tempfile.TemporaryDirectory(prefix="yui-copy-", dir=temp_dir()) as folder:
            snapshot = Path(folder) / "snapshot.gif"
            snapshot.write_bytes(payload)
            return _publish_output(snapshot, output, overwrite, cancelled)
    except OSError as exc:
        raise MediaError("Could not save the GIF. Check the output folder and free space.") from exc


def _publish_output(source: Path, output: Path, overwrite: bool, cancelled: Callable[[], bool]) -> Path:
    """Prepare the full GIF on the destination volume before publishing its name."""
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=output.parent, prefix=".yui-save-", suffix=".tmp")
    os.close(fd)
    staging = Path(name)
    try:
        shutil.copyfile(source, staging)
        with staging.open("r+b") as fh:
            os.fsync(fh.fileno())
        if cancelled():
            raise RenderCancelled()
        if overwrite:
            os.replace(staging, output)
            return output
        for _ in range(10_000):
            candidate = unique_path(output)
            try:
                # Windows rename fails if the destination exists, including a race
                # after unique_path(). Both names are on the same volume.
                if os.name == "nt":
                    os.rename(staging, candidate)
                else:
                    os.link(staging, candidate)
                return candidate
            except FileExistsError:
                continue
        raise MediaError("Too many GIFs have this name. Choose another output folder.")
    except OSError as exc:
        raise MediaError("Could not save the GIF. Check the output folder, free space, and whether the file is open.") from exc
    finally:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise MediaError("Too many GIFs have this name. Choose another output folder.")


def _writable(folder: Path) -> bool:
    if not is_local_path(folder):
        return False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=folder):
            pass
        return True
    except OSError:
        return False


def output_path(
    source: Path,
    out_dir: Optional[Path] = None,
    combined_count: int = 1,
    overwrite: bool = False,
) -> Path:
    folder = Path(out_dir) if out_dir else fallback_output_dir()
    if not _writable(folder):
        raise MediaError("The output folder is unavailable or not writable. Choose a folder in Settings.")
    stem = source.stem
    if combined_count > 1:
        stem = f"{stem} + {combined_count - 1} more"
    target = folder / f"{stem}.gif"
    if target.resolve() == source.resolve():
        target = folder / f"{stem} (yui).gif"
    return target if overwrite else unique_path(target)
