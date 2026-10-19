"""Reusable GIF options, kept locally without file paths or workflow preferences.

``PresetStore().names()`` lists four built-ins followed by saved names. ``apply``
changes only GIF options on the supplied Settings object. ``save_current`` raises
PresetError for a duplicate name unless ``overwrite=True`` is explicit. Built-in
names cannot be replaced or deleted. A successful save/delete is atomic; failed
writes leave the previous file and the in-memory presets unchanged.
``Custom`` is reserved for the UI's unsaved-options status and is never stored.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from .paths import data_dir, is_local_path
from .settings import Settings


OPTION_FIELDS = (
    "max_mb", "priority", "fps", "max_side", "dither", "loop", "speed", "boomerang",
    "aspect", "fit", "background", "transition", "transition_seconds", "image_seconds",
    "crop", "effect", "reverse", "palette_colors", "palette_mode",
)
MAX_SAVED_PRESETS = 50
MAX_NAME_LENGTH = 64
MAX_FILE_BYTES = 256 * 1024
_SCHEMA_VERSION = 1


class PresetError(ValueError):
    """An actionable preset error whose message never includes a profile path."""


def _options(settings: Settings) -> dict:
    # Sanitize a copy: neither saving nor loading a preset alters its caller.
    clean = replace(settings).sanitize()
    return {name: getattr(clean, name) for name in OPTION_FIELDS}


def _from_options(raw: dict) -> dict:
    settings = Settings()
    for name in OPTION_FIELDS:
        if name in raw:
            setattr(settings, name, raw[name])
    return _options(settings)


def _name(value: str) -> str:
    if not isinstance(value, str):
        raise PresetError("Enter a name for your preset.")
    name = value.strip()
    if not name or len(name) > MAX_NAME_LENGTH or not name.isprintable():
        raise PresetError("Use a preset name with 1 to 64 characters on one line.")
    return name


_BUILTINS = {
    "Balanced": _options(Settings()),
    "Small file": _options(Settings(max_mb=3, fps=12, max_side=480)),
    "Smooth motion": _options(Settings(priority="smooth", fps=25, max_side=640)),
    "Sticker": _options(Settings(max_mb=3, max_side=480, aspect="1:1", fit="fit", fps=15)),
}
BUILTIN_NAMES = tuple(_BUILTINS)
_RESERVED_NAMES = {name.casefold() for name in (*BUILTIN_NAMES, "Custom")}
BUILTIN_DESCRIPTIONS = {
    "Balanced": "Automatic quality and size, up to 10 MB.",
    "Small file": "A compact GIF, up to 3 MB and 480 px.",
    "Smooth motion": "Prefer fluid motion, up to 25 frames per second.",
    "Sticker": "A square canvas, up to 480 px and 3 MB.",
}


class PresetStore:
    """A local preset collection; instantiate again to read changes after restart."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else data_dir() / "presets.json"
        if not is_local_path(self.path):
            raise PresetError("A local folder is required for presets.")
        self._saved: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not is_local_path(self.path):
                return
            with self.path.open("rb") as stream:
                payload = stream.read(MAX_FILE_BYTES + 1)
            if len(payload) > MAX_FILE_BYTES:
                return
            raw = json.loads(payload.decode("utf-8"))
        except (OSError, ValueError, UnicodeError, RecursionError):
            return
        if (not isinstance(raw, dict) or type(raw.get("version")) is not int
                or raw.get("version") != _SCHEMA_VERSION):
            return
        presets = raw.get("presets")
        if not isinstance(presets, dict):
            return
        seen = set(_RESERVED_NAMES)
        for value, options in presets.items():
            try:
                name = _name(value)
            except PresetError:
                continue
            folded = name.casefold()
            if folded in seen or not isinstance(options, dict):
                continue
            self._saved[name] = _from_options(options)
            seen.add(folded)
            if len(self._saved) >= MAX_SAVED_PRESETS:
                break

    def names(self) -> list[str]:
        return [*BUILTIN_NAMES, *sorted(self._saved, key=str.casefold)]

    def is_builtin(self, name: str) -> bool:
        return isinstance(name, str) and name.strip().casefold() in {
            value.casefold() for value in BUILTIN_NAMES
        }

    def _resolve(self, name: str) -> str:
        folded = _name(name).casefold()
        for candidate in (*BUILTIN_NAMES, *self._saved):
            if candidate.casefold() == folded:
                return candidate
        raise PresetError("That preset is no longer available.")

    def get(self, name: str) -> dict:
        key = self._resolve(name)
        options = _BUILTINS[key] if key in _BUILTINS else self._saved[key]
        return dict(options)

    def apply(self, settings: Settings, name: str) -> Settings:
        for option, value in self.get(name).items():
            setattr(settings, option, value)
        return settings

    def save_current(self, name: str, settings: Settings, *, overwrite: bool = False) -> str:
        name = _name(name)
        if name.casefold() == "custom":
            raise PresetError("Custom is reserved for unsaved options. Choose a different name.")
        if self.is_builtin(name):
            raise PresetError("Built-in presets are reserved. Choose a different name.")
        existing = next((key for key in self._saved if key.casefold() == name.casefold()), None)
        if existing is not None:
            if not overwrite:
                raise PresetError("A preset with that name already exists. Choose another name or replace it.")
            name = existing
        elif len(self._saved) >= MAX_SAVED_PRESETS:
            raise PresetError("You can save up to 50 presets. Delete one before adding another.")
        changed = dict(self._saved)
        changed[name] = _options(settings)
        self._persist(changed)
        self._saved = changed
        return name

    def delete(self, name: str) -> None:
        key = self._resolve(name)
        if key in _BUILTINS:
            raise PresetError("Built-in presets cannot be deleted.")
        changed = dict(self._saved)
        del changed[key]
        self._persist(changed)
        self._saved = changed

    def _persist(self, presets: dict[str, dict]) -> None:
        if not is_local_path(self.path):
            raise PresetError("A local folder is required for presets.")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".presets-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": _SCHEMA_VERSION, "presets": presets}, stream,
                          indent=2, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise PresetError("Your presets could not be saved. Check the available disk space and try again.") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
