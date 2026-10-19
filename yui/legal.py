"""Legal documents bundled with the app (terms, privacy, third-party licenses)."""

from __future__ import annotations

from pathlib import Path

from . import APP_NAME, AUTHOR, TERMS_VERSION, VERSION, YEAR
from .paths import resource

DOCUMENTS = {
    "terms": "TERMS.txt",
    "privacy": "PRIVACY.txt",
    "notices": "THIRD_PARTY_NOTICES.txt",
}


def _fill(text: str) -> str:
    for key, value in {
        "{APP_NAME}": APP_NAME,
        "{VERSION}": VERSION,
        "{AUTHOR}": AUTHOR,
        "{YEAR}": YEAR,
        "{TERMS_VERSION}": TERMS_VERSION,
    }.items():
        text = text.replace(key, value)
    return text


def document(key: str) -> str:
    path = resource("legal", DOCUMENTS[key])
    try:
        return _fill(path.read_text(encoding="utf-8"))
    except OSError:
        return f"The {key} document is missing from this copy of {APP_NAME}."


def license_files() -> list[tuple[str, Path]]:
    """(display name, path) for every bundled license text."""
    folder = resource("legal", "licenses")
    try:
        files = sorted(folder.glob("*.txt"), key=lambda p: p.name.lower())
    except OSError:
        return []
    labels = {"packaging-Apache-2.0": "packaging (Apache 2.0)",
              "packaging-BSD-2-Clause": "packaging (BSD)", "FFmpeg-GPL-3.0": "FFmpeg (GPLv3)",
              "imageio-ffmpeg-BSD-2-Clause": "imageio-ffmpeg", "Tcl-Tk-BSD-style": "Tcl/Tk"}
    return [(labels.get(p.stem, p.stem.split("-", 1)[0]), p) for p in files]


def read_license(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "This license file could not be read."
