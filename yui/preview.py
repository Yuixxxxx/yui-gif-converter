"""Responsive, bounded GIF playback for the result preview."""

from __future__ import annotations

import io
import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import customtkinter as ctk
from PIL import Image

from .paths import is_local_path
from .engine import HARD_CAP_BYTES, MAX_ANIMATION_FRAMES, MediaError, _check_canvas, _open_image


@dataclass(frozen=True)
class _Frame:
    image: Image.Image
    seconds: float
    source_size: tuple[int, int]


@dataclass(frozen=True)
class _Info:
    frames: int


class _Unavailable:
    pass


class GifPlayer:
    """Decode off the Tk thread and keep preview memory independent of GIF length.

    A small ready queue limits how far decoding can run ahead. Short animations
    reuse a bounded cache of small PIL frames; longer ones are decoded again for
    each loop. Only the Tk thread creates CTkImage objects or accesses widgets.
    """

    READY_FRAMES = 3
    CACHE_BYTES = 32 * 1024 * 1024
    MAX_RASTER_SIDE = 768
    MAX_SOURCE_PIXELS = 4_194_304
    POLL_MS = 8

    def __init__(self, label: ctk.CTkLabel, box: ctk.CTkFrame) -> None:
        self.label = label
        self.box = box
        self.frames = 0
        self.job = None
        self._current = None
        self._ready: Optional[queue.Queue] = None
        self._cancel: Optional[threading.Event] = None
        self._worker: Optional[threading.Thread] = None
        self._generation = 0
        self._deadline: Optional[float] = None
        self._path: Optional[Path] = None
        self._paused = False
        self._remaining = 0.0
        self._stats: dict[str, int | bool] = {}

    @property
    def playable(self) -> bool:
        """Whether an animated GIF has a displayed frame to control."""
        return self.frames > 1 and self._current is not None and self._cancel is not None

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> bool:
        """Freeze the displayed frame, retaining its remaining encoded delay.

        Controls become usable after the first animated frame is displayed.
        Loading and still images are left alone so neither can get stranded.
        """
        if not self.playable or self._paused:
            return False
        self._paused = True
        self._remaining = max(0.0, (self._deadline or time.monotonic()) - time.monotonic())
        self._cancel_timer()
        return True

    def resume(self) -> bool:
        """Continue at the saved frame delay without catching up paused time."""
        if not self.playable or not self._paused:
            return False
        self._paused = False
        self._deadline = time.monotonic() + self._remaining
        self._remaining = 0.0
        self._schedule(math.ceil(max(0, self._deadline - time.monotonic()) * 1000))
        return True

    def restart(self) -> bool:
        """Reload the chosen animation from its first frame and start playing."""
        if not self.playable or self._path is None:
            return False
        path = self._path
        self.load(path)
        return True

    def load(self, path: Path) -> None:
        self.stop()
        path = Path(path)
        if not is_local_path(path):
            self.label.configure(text="Preview requires a file on a local drive", image=None)
            return
        self._path = path
        self.label.configure(text="Loading preview…", image=None)
        width, height = self._box_size()
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self.box)
        except Exception:
            scaling = 1.0
        # Decode enough pixels for the current display, with a hard ceiling.
        raster_box = (max(1, int(width * scaling)), max(1, int(height * scaling)))
        ready = queue.Queue(maxsize=self.READY_FRAMES)
        cancel = threading.Event()
        stats = {"cache_bytes": 0, "queue_max": 0, "decoded_frames": 0, "fully_cached": False,
                 "raster_max": 0}
        self._ready, self._cancel, self._stats = ready, cancel, stats
        self._worker = threading.Thread(
            target=self._decode, args=(path, raster_box, ready, cancel, stats), daemon=True,
            name="Yui GIF preview",
        )
        self._worker.start()
        self._schedule(self.POLL_MS)

    @classmethod
    def _put(cls, ready: queue.Queue, cancel: threading.Event, value, stats: dict) -> bool:
        while not cancel.is_set():
            try:
                ready.put(value, timeout=0.05)
                stats["queue_max"] = max(stats["queue_max"], ready.qsize())
                return True
            except queue.Full:
                pass
        return False

    @classmethod
    def _decode(cls, path: Path, raster_box: tuple[int, int], ready: queue.Queue,
                cancel: threading.Event, stats: dict) -> None:
        try:
            # The original file is closed before any frames reach the UI.
            with path.open("rb") as file:
                data = file.read(HARD_CAP_BYTES + 1)
            if not data or len(data) > HARD_CAP_BYTES:
                raise MediaError("Preview file exceeds the GIF size limit.")
            if cancel.is_set():
                return
            with _open_image(io.BytesIO(data)) as source:
                if source.format != "GIF" or source.width * source.height > cls.MAX_SOURCE_PIXELS:
                    raise MediaError("Preview requires a GIF with a supported canvas.")
                frames = int(getattr(source, "n_frames", 1) or 1)
                if frames > MAX_ANIMATION_FRAMES:
                    raise MediaError("Preview animation has too many frames.")
                if cancel.is_set() or not cls._put(ready, cancel, _Info(frames), stats):
                    return
                source_size = source.size
                ratio = min(raster_box[0] / source.width, raster_box[1] / source.height,
                            cls.MAX_RASTER_SIDE / max(source.size), 1.0)
                raster_size = (max(1, int(source.width * ratio)), max(1, int(source.height * ratio)))
                cache: list[_Frame] = []
                cache_bytes = 0
                can_cache = True
                first_loop = True
                while not cancel.is_set():
                    for index in range(frames):
                        if cancel.is_set():
                            return
                        source.seek(index)
                        _check_canvas(*source.size)
                        if source.width * source.height > cls.MAX_SOURCE_PIXELS:
                            raise MediaError("Preview canvas is too large.")
                        # Preserve each encoded GIF delay, including 10 ms frames.
                        seconds = max(10, int(source.info.get("duration", 100) or 100)) / 1000
                        image = source.convert("RGBA")
                        if image.size != raster_size:
                            image = image.resize(raster_size, Image.Resampling.BILINEAR)
                        packet = _Frame(image, seconds, source_size)
                        stats["decoded_frames"] += 1
                        stats["raster_max"] = max(stats["raster_max"], max(image.size))
                        if first_loop and can_cache:
                            frame_bytes = image.width * image.height * 4
                            if cache_bytes + frame_bytes <= cls.CACHE_BYTES:
                                cache.append(packet)
                                cache_bytes += frame_bytes
                                stats["cache_bytes"] = cache_bytes
                            else:
                                # Never retain a growing partial cache for a long GIF.
                                cache.clear()
                                cache_bytes = 0
                                stats["cache_bytes"] = 0
                                can_cache = False
                        if not cls._put(ready, cancel, packet, stats):
                            return
                    if frames == 1:
                        return
                    if first_loop and can_cache:
                        stats["fully_cached"] = True
                        while not cancel.is_set():
                            for packet in cache:
                                if not cls._put(ready, cancel, packet, stats):
                                    return
                        return
                    first_loop = False
        except Exception:
            if not cancel.is_set():
                cls._put(ready, cancel, _Unavailable(), stats)

    def _box_size(self) -> tuple[float, float]:
        try:
            scaling = ctk.ScalingTracker.get_widget_scaling(self.box)
        except Exception:
            scaling = 1.0
        width = self.box.winfo_width() / scaling - 24
        height = self.box.winfo_height() / scaling - 24
        return max(120.0, width), max(90.0, height)

    def _schedule(self, delay: int) -> None:
        if self._paused:
            return
        generation = self._generation
        self.job = self.label.after(max(1, delay), lambda: self._tick(generation))

    def _next_frame(self):
        if self._ready is None:
            return None
        while True:
            try:
                packet = self._ready.get_nowait()
            except queue.Empty:
                return None
            if isinstance(packet, _Info):
                self.frames = packet.frames
            else:
                return packet

    def _tick(self, generation: int) -> None:
        if generation != self._generation or self._cancel is None or self._cancel.is_set():
            return
        self.job = None
        if self._paused:
            return
        now = time.monotonic()
        if self._deadline is not None and now < self._deadline:
            self._schedule(math.ceil((self._deadline - now) * 1000))
            return
        packet = self._next_frame()
        if packet is None:
            self._schedule(self.POLL_MS)
            return
        if isinstance(packet, _Unavailable):
            self.stop()
            self.label.configure(text="Preview unavailable", image=None)
            return
        if self._deadline is None:
            self._deadline = now + packet.seconds
        else:
            self._deadline += packet.seconds
            # Catch up after a busy UI without permanently extending the GIF.
            # Bound this work so a delayed preview still yields to mouse events.
            for _ in range(self.READY_FRAMES):
                if self._deadline > now:
                    break
                following = self._next_frame()
                if following is None:
                    break
                if isinstance(following, _Unavailable):
                    self.stop()
                    self.label.configure(text="Preview unavailable", image=None)
                    return
                packet = following
                self._deadline += packet.seconds
        width, height = self._box_size()
        source_width, source_height = packet.source_size
        ratio = min(width / source_width, height / source_height, 2.0)
        size = (max(1, int(source_width * ratio)), max(1, int(source_height * ratio)))
        self._current = ctk.CTkImage(light_image=packet.image, dark_image=packet.image, size=size)
        self.label.configure(image=self._current, text="")
        if self.frames == 1:
            return
        self._schedule(math.ceil(max(0, self._deadline - time.monotonic()) * 1000))

    def _cancel_timer(self) -> None:
        if self.job is not None:
            try:
                self.label.after_cancel(self.job)
            except Exception:
                pass
        self.job = None

    def stop(self) -> None:
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        self._cancel_timer()
        self._cancel = None
        self._ready = None
        self._current = None
        self._deadline = None
        self._path = None
        self._paused = False
        self._remaining = 0.0
        self.frames = 0
        try:
            # CTkLabel's None value removes its Python image reference without
            # clearing the underlying Tk image. An empty value clears Tk too,
            # so loading another GIF cannot refer to a destroyed PhotoImage.
            if isinstance(self.label, ctk.CTkLabel):
                self.label.configure(image="")
            self.label.configure(image=None)
        except Exception:
            pass
