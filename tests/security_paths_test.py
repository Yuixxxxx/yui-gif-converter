"""Check local file boundaries and hostile preference-file recovery.

All files and the optional real Windows junction are synthetic fixtures inside
the supplied work folder. No remote shares, account data or clipboard is opened.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yui import engine
from yui.paths import data_dir, is_local_path
from yui.presets import BUILTIN_NAMES, PresetStore
from yui.settings import Settings


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="security-paths-", dir=work))
    os.environ["APPDATA"] = str(fixture / "profile")
    os.environ["LOCALAPPDATA"] = str(fixture / "local-profile")
    checks, limitations = [], []

    def check(condition, label):
        assert condition, label
        checks.append(label)

    forbidden = (
        r"\\invalid-host\share\clip.mp4", r"//invalid-host/share/clip.mp4",
        r"\\?\UNC\invalid-host\share\clip.mp4", r"\\?\C:\clip.mp4", r"\\.\NUL",
        r"\??\UNC\invalid-host\share\clip.mp4", r"\??\C:\clip.mp4", "C:relative.mp4",
        str(fixture / "clip.mp4") + ":hidden.gif",
        *(str(fixture / name) for name in ("NUL.mp4", "AUX.gif", "PRN.png", "CON.gif",
                                         "COM1.mp4", "COM9.png", "LPT1.gif", "LPT9.jpg",
                                         "NUL .mp4", "CONIN$.gif", "CONOUT$.mp4", "COM\u00b9.gif", "LPT\u00b2.png")),
    )
    for value in forbidden:
        with patch("yui.paths.os.lstat", side_effect=AssertionError("Forbidden path was dereferenced")), \
             patch("pathlib.Path.resolve", side_effect=AssertionError("Forbidden path was resolved")):
            check(not is_local_path(Path(value)), "forbidden path is rejected before access")
    for value in forbidden:
        with patch("pathlib.Path.is_file", side_effect=AssertionError("Forbidden input was opened")):
            try:
                engine.probe(Path(value))
            except engine.MediaError:
                pass
            else:
                raise AssertionError("Forbidden media path reached the decoder")
    check(True, "media probing rejects unsafe paths before opening them")
    if os.name == "nt":
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4), \
             patch("yui.paths.os.lstat", side_effect=AssertionError("Mapped drive was dereferenced")):
            check(not is_local_path(Path("Z:/clip.mp4")), "mapped network drive is rejected before metadata access")

    source = fixture / "local" / "clip.mp4"
    source.parent.mkdir()
    source.write_bytes(b"local synthetic fixture")
    with patch("pathlib.Path.resolve", side_effect=AssertionError("Guard followed a path")):
        check(is_local_path(source), "ordinary local files pass without following links")
        check(is_local_path(fixture / "new-folder" / "new.gif"), "new local destinations are permitted")
    check(is_local_path(Path(os.path.relpath(source))), "ordinary relative paths resolve lexically to a local drive")

    original_lstat = os.lstat
    link = fixture / "linked-profile"
    touched = []

    def guarded_lstat(path, *args, **kwargs):
        candidate = Path(path)
        touched.append(candidate)
        if candidate == link:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        if link in candidate.parents:
            raise AssertionError("A descendant of a reparse point was accessed")
        return original_lstat(path, *args, **kwargs)

    with patch("yui.paths.os.lstat", side_effect=guarded_lstat), \
         patch("pathlib.Path.resolve", side_effect=AssertionError("Remote link was resolved")):
        check(not is_local_path(link / "nested" / "clip.mp4"),
              "reparse ancestor is rejected without inspecting its descendants")
    check(link in touched and not any(link in candidate.parents for candidate in touched),
          "no-follow metadata walk stops at the first reparse point")
    file_link = fixture / "linked-settings.json"
    def guarded_file_lstat(path, *args, **kwargs):
        if Path(path) == file_link:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path, *args, **kwargs)
    with patch("yui.paths.os.lstat", side_effect=guarded_file_lstat), \
         patch("pathlib.Path.resolve", side_effect=AssertionError("File symlink was followed")), \
         patch("pathlib.Path.open", side_effect=AssertionError("File symlink was opened")):
        check(not is_local_path(file_link), "file symlinks are rejected without opening their targets")
        with patch.object(Settings, "path", return_value=file_link):
            check(Settings.load() == Settings(), "a linked preference file recovers before any read")
            with patch("yui.settings.tempfile.NamedTemporaryFile", side_effect=AssertionError("Linked preferences were written")):
                Settings().save()
        check(True, "a linked preference file is not written")

    if os.name == "nt":
        target = fixture / "junction-target"
        target.mkdir()
        junction = fixture / "real-junction"
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        script = ("New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''")
                  + "' -Target '" + str(target).replace("'", "''") + "' -ErrorAction Stop | Out-Null")
        try:
            created = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                                     capture_output=True, timeout=15, creationflags=engine.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            limitations.append("Real junction creation was unavailable; mocked reparse boundary was tested.")
        else:
            if created.returncode:
                limitations.append("Real junction creation was unavailable; mocked reparse boundary was tested.")
            else:
                check(bool(getattr(original_lstat(junction), "st_file_attributes", 0) & 0x400),
                      "synthetic fixture is a real Windows reparse point")
                with patch("pathlib.Path.resolve", side_effect=AssertionError("Junction was followed")):
                    check(not is_local_path(junction / "clip.mp4"), "real local junction is rejected without following it")
                with patch.dict(os.environ, {"APPDATA": str(junction)}):
                    check(data_dir() == fixture / "local-profile" / "YuiGifConverter",
                          "junction profile falls back to the local application data folder")

    preference = fixture / "settings.json"
    for contents in (
        b'{"fps":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}",
        b'{"theme":"light","padding":"' + b"x" * (256 * 1024) + b'"}',
        b"\xff\xfe", b"[]", b"not json",
    ):
        preference.write_bytes(contents)
        with patch.object(Settings, "path", return_value=preference):
            check(Settings.load() == Settings(), "hostile or malformed preferences recover to defaults")
    preference.write_text(json.dumps({"theme": "light", "fps": "invalid", "speed": 2,
                                      "output_dir": [], "overwrite": "false"}), encoding="utf-8")
    with patch.object(Settings, "path", return_value=preference):
        recovered = Settings.load()
    check(recovered.theme == "light" and recovered.fps == 0 and recovered.speed == 2
          and recovered.output_dir == "" and recovered.overwrite is False,
          "valid preferences survive independent damaged fields")

    preset = fixture / "presets.json"
    for contents in (
        b'{"version":1,"presets":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}",
        b'{"version":1,"presets":{},"padding":"' + b"x" * (256 * 1024) + b'"}',
        b"\xff\xfe", b"not json",
    ):
        preset.write_bytes(contents)
        check(PresetStore(preset).names() == list(BUILTIN_NAMES), "hostile presets recover to built-in choices")
    preset.write_text(json.dumps({"version": 1, "presets": {"Local": {"fps": 15}}}), encoding="utf-8")
    check(PresetStore(preset).get("Local")["fps"] == 15, "valid preset files still load after defensive limits")

    report = {"ok": True, "count": len(checks), "checks": checks, "limitations": limitations,
              "remote_files_accessed": False, "real_clipboard_accessed": False}
    (work / "security-paths-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
