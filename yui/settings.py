"""User options, saved to %APPDATA%\\YuiGifConverter\\settings.json between launches."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

from .engine import ASPECTS, DITHERS, HARD_CAP_MB, PRIORITIES, TRANSITIONS, RenderOptions
from .paths import data_dir, is_local_path

MAX_SETTINGS_BYTES = 256 * 1024


@dataclass
class Settings:
    # GIF options
    max_mb: float = 10
    priority: str = "auto"
    fps: int = 0
    max_side: int = 0
    dither: str = "auto"
    loop: int = 0
    speed: float = 1.0
    boomerang: bool = False
    aspect: str = "auto"
    fit: str = "fit"
    background: str = "#000000"
    transition: str = "none"
    transition_seconds: float = 0.5
    image_seconds: float = 2.0
    crop: tuple | None = None  # Normalized rectangle, shared by each imported clip.
    effect: str = "none"
    reverse: bool = False
    palette_colors: int = 256
    palette_mode: str = "auto"
    # Workflow
    mode: str = "combine"  # combine | separate
    output_dir: str = ""  # empty = the local Yui GIFs folder
    overwrite: bool = False
    open_when_done: bool = False
    sound: bool = True
    theme: str = "dark"  # dark | light | system
    button_animations: bool = True
    private_filenames: bool = True
    accepted_terms: str = ""

    def render_options(self) -> RenderOptions:
        return RenderOptions(
            max_mb=self.max_mb,
            priority=self.priority,
            fps=self.fps,
            max_side=self.max_side,
            dither=self.dither,
            loop=self.loop,
            speed=self.speed,
            boomerang=self.boomerang,
            aspect=self.aspect,
            fit=self.fit,
            background=self.background,
            transition=self.transition,
            transition_seconds=self.transition_seconds,
            crop=self.crop,
            effect=self.effect,
            reverse=self.reverse,
            palette_colors=self.palette_colors,
            palette_mode=self.palette_mode,
        )

    def sanitize(self) -> "Settings":
        defaults = Settings()
        def number(name, lo, hi, integer=False):
            try:
                raw = getattr(self, name)
                value = float(raw) if not isinstance(raw, bool) else float("nan")
                if not math.isfinite(value):
                    raise ValueError()
                value = max(lo, min(hi, value))
            except (TypeError, ValueError, OverflowError):
                value = getattr(defaults, name)
            setattr(self, name, int(value) if integer else value)

        for name, lo, hi, integer in (
            ("max_mb", .5, HARD_CAP_MB, False), ("fps", 0, 50, True),
            ("max_side", 0, 1920, True), ("loop", -1, 100, True),
            ("speed", .1, 8, False), ("transition_seconds", .1, 3, False),
            ("image_seconds", .1, 60, False),
            ("palette_colors", 4, 256, True),
        ):
            number(name, lo, hi, integer)
        for name, choices in (
            ("priority", PRIORITIES), ("dither", (*DITHERS, "auto")),
            ("aspect", (*ASPECTS, "auto")), ("fit", ("fit", "fill")),
            ("transition", TRANSITIONS), ("mode", ("combine", "separate")),
            ("theme", ("dark", "light", "system")),
            ("effect", ("none", "grayscale", "sepia", "vivid", "warm", "cool", "invert")),
            ("palette_mode", ("auto", "full")),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or value not in choices:
                setattr(self, name, getattr(defaults, name))
        for name in ("boomerang", "reverse", "overwrite", "open_when_done", "sound", "button_animations", "private_filenames"):
            if not isinstance(getattr(self, name), bool):
                setattr(self, name, getattr(defaults, name))
        if self.crop is not None:
            try:
                if not isinstance(self.crop, (list, tuple)) or len(self.crop) != 4 or any(isinstance(n, bool) for n in self.crop):
                    raise ValueError()
                left, top, width, height = (float(n) for n in self.crop)
                if not all(math.isfinite(n) for n in (left, top, width, height)) or not (
                    0 <= left < 1 and 0 <= top < 1 and 0 < width <= 1 and 0 < height <= 1
                    and left + width <= 1.000000001 and top + height <= 1.000000001
                ):
                    raise ValueError()
                self.crop = (left, top, min(width, 1 - left), min(height, 1 - top))
                if self.crop == (0., 0., 1., 1.):
                    self.crop = None
            except (TypeError, ValueError, OverflowError):
                self.crop = None
        for name in ("output_dir", "accepted_terms"):
            value = getattr(self, name)
            if not isinstance(value, str) or "\x00" in value or len(value) > (32760 if name == "output_dir" else 128):
                setattr(self, name, getattr(defaults, name))
        if not isinstance(self.background, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", self.background):
            self.background = defaults.background
        return self

    @classmethod
    def path(cls):
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        try:
            path = cls.path()
            if not is_local_path(path):
                return cls()
            with path.open("rb") as stream:
                payload = stream.read(MAX_SETTINGS_BYTES + 1)
            if len(payload) > MAX_SETTINGS_BYTES:
                return cls()
            raw = json.loads(payload.decode("utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            return cls()
        known = {f.name: f.type for f in fields(cls)}
        settings = cls()
        for key, value in raw.items() if isinstance(raw, dict) else []:
            if key in known:
                setattr(settings, key, value)
        return settings.sanitize()

    def save(self) -> None:
        temporary = None
        try:
            path = self.path()
            if not is_local_path(path):
                return
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".settings-", suffix=".tmp", delete=False) as fh:
                temporary = Path(fh.name)
                json.dump(asdict(self.sanitize()), fh, indent=2, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temporary, path)
        except (OSError, RuntimeError, ValueError):
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def reset_options(self) -> None:
        """Restore every GIF option to its default but keep workflow choices and terms acceptance."""
        defaults = Settings()
        for name in (
            "max_mb", "priority", "fps", "max_side", "dither", "loop", "speed", "boomerang",
            "aspect", "fit", "background", "transition", "transition_seconds", "image_seconds",
            "crop", "effect", "reverse", "palette_colors", "palette_mode",
        ):
            setattr(self, name, getattr(defaults, name))


class SettingsWriter:
    """Coalesce preference edits and serialize atomic saves off the UI thread."""

    def __init__(self, delay: float = .2) -> None:
        self._delay = delay
        self._condition = threading.Condition()
        self._pending = None
        self._closing = False
        self._thread = threading.Thread(target=self._run, name="Yui preferences", daemon=True)
        self._thread.start()

    def submit(self, settings: Settings) -> None:
        with self._condition:
            if not self._closing:
                self._pending = (replace(settings), time.monotonic() + self._delay)
                self._condition.notify()

    def close(self, settings: Settings) -> None:
        """Flush the last snapshot before exit, including edits inside the debounce window."""
        with self._condition:
            if not self._closing:
                self._pending = (replace(settings), 0)
                self._closing = True
                self._condition.notify()
        self._thread.join()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None:
                    if self._closing:
                        return
                    self._condition.wait()
                snapshot, deadline = self._pending
                remaining = deadline - time.monotonic()
                if remaining > 0 and not self._closing:
                    self._condition.wait(remaining)
                    continue
                self._pending = None
            snapshot.save()
