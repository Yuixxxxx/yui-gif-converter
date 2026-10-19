"""Where Yui finds its bundled files and keeps its own data."""

from __future__ import annotations

import os
import sys
import ctypes
import re
import stat
from functools import lru_cache
from pathlib import Path

from . import APP_ID


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Folder holding bundled read-only files (legal texts, icon, ffmpeg)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def resource(*parts: str) -> Path:
    return resource_dir().joinpath(*parts)


def is_local_path(path: Path) -> bool:
    """Reject special paths and no-follow links before filesystem access.

    Do not resolve an untrusted path: resolving a junction can itself contact a
    network share. Inspect each ancestor without following it instead.
    """
    value = str(path).replace("/", "\\")
    if not value or value.startswith(("\\\\", "\\??\\")) or any(ord(c) < 32 for c in value):
        return False
    if os.name == "nt":
        candidate = Path(path)
        if candidate.drive and not candidate.root:
            return False  # C:relative uses an implicit per-drive working folder.
        for part in candidate.parts[1:] if candidate.anchor else candidate.parts:
            if ":" in part or part.endswith((" ", ".")) and part not in (".", ".."):
                return False
            device = part.split(".", 1)[0].rstrip(" ").upper()
            if device in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", device):
                return False
        root = candidate.anchor or Path.cwd().anchor
        if root and ctypes.windll.kernel32.GetDriveTypeW(root) == 4:
            return False
    absolute = Path(os.path.abspath(path))
    # lstat on the link itself is safe; never descend through that link.
    for part in (*reversed(absolute.parents), absolute):
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return False
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def data_dir() -> Path:
    """Per-user folder for settings and logs (%APPDATA%\\YuiGifConverter)."""
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    path = Path(base) / APP_ID
    if not is_local_path(path):
        path = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")) / APP_ID
    if not is_local_path(path):
        raise RuntimeError("A local folder is required for Yui settings.")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def log_path() -> Path:
    return data_dir() / "error.log"


def temp_dir() -> Path:
    """Use a checked local app folder instead of following arbitrary TMP/TEMP values."""
    path = data_dir() / "Temp"
    if not is_local_path(path):
        raise RuntimeError("A local temporary folder is required.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def fallback_output_dir() -> Path:
    """Local default output folder, outside cloud-synced Documents/Pictures folders."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    path = Path(base) / APP_ID / "GIFs"
    if not is_local_path(path):
        raise RuntimeError("Choose an output folder on a local drive.")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_ffmpeg() -> str:
    bundled = resource("bin", "ffmpeg.exe")
    if not is_local_path(bundled):
        raise RuntimeError("The bundled converter must be on a local drive.")
    try:
        info = bundled.stat()
        _verify_ffmpeg(str(bundled), info.st_size, info.st_mtime_ns)
        return str(bundled)
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "The bundled converter is missing or changed. Reinstall Yui, or run tools/fetch_ffmpeg.py when using source."
        ) from exc


@lru_cache(maxsize=2)
def _verify_ffmpeg(filename: str, size: int, modified: int) -> None:
    import hashlib
    from .runtime import FFMPEG_SHA256
    hasher = hashlib.sha256()
    with open(filename, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    if hasher.hexdigest() != FFMPEG_SHA256:
        raise ValueError("Bundled converter checksum mismatch")
