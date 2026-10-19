# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

import tkinterdnd2
from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)
from yui.paths import get_ffmpeg
FFMPEG = Path(get_ffmpeg())
TKDND_PLATFORM = "win-x64-tcl9"
TKDND = Path(tkinterdnd2.__file__).parent / "tkdnd" / TKDND_PLATFORM
if not TKDND.is_dir():
    raise RuntimeError("This release requires tkinterdnd2's Windows x64 Tcl 9 library.")

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[(str(FFMPEG), "bin")],
    datas=[
        (str(ROOT / "legal"), "legal"),
        (str(ROOT / "assets"), "assets"),
        (str(TKDND), "tkinterdnd2/tkdnd/" + TKDND_PLATFORM),
        *collect_data_files("customtkinter"),
    ],
    hiddenimports=["tkinterdnd2"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["imageio_ffmpeg", "numpy", "scipy"],
    noarchive=False,
)

# Hooks may collect tkdnd for other platforms. Keep only this release's architecture.
def keep_platform(entry):
    name = entry[0].replace("\\", "/")
    return "tkinterdnd2/tkdnd/" not in name or "tkinterdnd2/tkdnd/" + TKDND_PLATFORM + "/" in name

a.datas = [entry for entry in a.datas if keep_platform(entry)]
a.binaries = [
    ("bin/ffmpeg.exe", source, kind) if Path(source) == FFMPEG else (name, source, kind)
    for name, source, kind in a.binaries if keep_platform((name, source, kind))
]
if sum(name.replace("\\", "/") == "bin/ffmpeg.exe" for name, _, _ in a.binaries) != 1:
    raise RuntimeError("The release must contain exactly one bin/ffmpeg.exe.")

pyz = PYZ(a.pure)
splash = Splash(
    str(ROOT / "assets" / "splash.png"),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    always_on_top=False,
)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    splash,
    splash.binaries,
    [],
    name="YuiGifConverter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    icon=str(ROOT / "assets" / "icon.ico"),
    version=str(ROOT / "version_info.txt"),
)
