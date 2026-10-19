"""Check preset isolation, damaged data recovery and atomic persistence.

Usage: python tests/presets_test.py <isolated-work-folder>
No user media, real profile files, browser or other accounts are accessed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from yui.presets import (
    BUILTIN_NAMES, MAX_FILE_BYTES, MAX_SAVED_PRESETS, OPTION_FIELDS, PresetError, PresetStore,
)
from yui.settings import Settings


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    fixtures = Path(tempfile.mkdtemp(prefix="preset-test-", dir=work))
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    checks = []

    def check(condition, name):
        assert condition, name
        checks.append(name)

    def reject(callback, name):
        try:
            callback()
        except PresetError as exc:
            check(str(work) not in str(exc), name)
        else:
            raise AssertionError(name)

    store = PresetStore(fixtures / "main" / "presets.json")
    check(store.names() == list(BUILTIN_NAMES), "missing file supplies four useful built-in presets")
    check(store.get("Balanced") == {name: getattr(Settings(), name) for name in OPTION_FIELDS},
          "Balanced restores every GIF option to defaults")
    check(store.get("Small file")["max_mb"] == 3 and store.get("Small file")["max_side"] == 480,
          "Small file limits size and resolution")
    check(store.get("Smooth motion")["fps"] == 25 and store.get("Smooth motion")["priority"] == "smooth",
          "Smooth motion prefers frame rate")
    check(store.get("Sticker")["aspect"] == "1:1" and store.get("Sticker")["fit"] == "fit",
          "Sticker fits source media within a square canvas")

    private_fields = {
        "output_dir": "PRIVATE_FOLDER_MARKER", "accepted_terms": "PRIVATE_TERMS_MARKER",
        "theme": "light", "mode": "separate", "overwrite": True, "sound": False,
        "open_when_done": True, "button_animations": False,
    }
    custom = Settings(fps=23, image_seconds=4.5, speed=1.25, **private_fields)
    before = asdict(custom)
    check(store.save_current("  My clip  ", custom) == "My clip", "saved names trim surrounding spaces")
    encoded = store.path.read_text(encoding="utf-8")
    payload = json.loads(encoded)
    saved = payload["presets"]["My clip"]
    check(set(saved) == set(OPTION_FIELDS) and all(field not in saved for field in private_fields),
          "presets persist only GIF options")
    check("PRIVATE_FOLDER_MARKER" not in encoded and "PRIVATE_TERMS_MARKER" not in encoded,
          "profile paths and consent values are excluded from stored presets")
    check(asdict(custom) == before, "saving does not mutate current settings")
    custom.fps = 11
    check(store.get("My clip")["fps"] == 23, "saved options are a snapshot")
    returned = store.get("My clip")
    returned["fps"] = 1
    check(store.get("My clip")["fps"] == 23, "reading cannot mutate stored options")

    target = Settings(**private_fields)
    check(store.apply(target, "MY CLIP") is target and target.fps == 23 and target.image_seconds == 4.5,
          "apply restores render options and image duration case-insensitively")
    check(all(getattr(target, name) == value for name, value in private_fields.items()),
          "apply preserves output folder, workflow, consent and appearance")
    store.apply(target, "Balanced")
    check(all(getattr(target, name) == getattr(Settings(), name) for name in OPTION_FIELDS)
          and all(getattr(target, name) == value for name, value in private_fields.items()),
          "Balanced reset preserves workflow and privacy choices")
    restarted = PresetStore(store.path)
    check(restarted.get("My clip") == store.get("My clip"), "saved options reload after restart")
    reject(lambda: store.save_current("my CLIP", Settings()), "duplicate names require explicit replacement")
    check(store.save_current("my CLIP", Settings(fps=17), overwrite=True) == "My clip"
          and store.get("My clip")["fps"] == 17, "explicit replacement keeps one canonical saved name")
    for reserved in ("Balanced", " small FILE ", "SMOOTH MOTION", "Sticker"):
        reject(lambda reserved=reserved: store.save_current(reserved, Settings(), overwrite=True),
               "built-in name is protected: " + reserved.strip())
    reject(lambda: store.delete("Balanced"), "built-in deletion is rejected")
    reject(lambda: store.save_current("  cUsToM  ", Settings(), overwrite=True),
           "Custom cannot shadow the unsaved-options control")
    reject(lambda: store.get("Missing"), "missing preset produces a friendly error")
    for invalid in ("", " ", "x" * 65, "two\nlines", "bad\x00name", [], None):
        reject(lambda invalid=invalid: store.save_current(invalid, Settings()), "invalid preset name is rejected")

    old_file = store.path.read_bytes()
    old_names = store.names()
    with patch("yui.presets.os.replace", side_effect=OSError("simulated full disk")):
        reject(lambda: store.save_current("Cannot save", Settings()), "failed save reports a friendly error")
    check(store.path.read_bytes() == old_file and store.names() == old_names
          and not list(store.path.parent.glob(".presets-*.tmp")),
          "failed atomic replace preserves file and memory and cleans temporary files")
    with patch("yui.presets.os.fsync", side_effect=OSError("simulated flush failure")):
        reject(lambda: store.save_current("My clip", Settings(fps=19), overwrite=True),
               "failed flush reports a friendly error")
    check(store.path.read_bytes() == old_file and store.get("My clip")["fps"] == 17
          and not list(store.path.parent.glob(".presets-*.tmp")),
          "failed flush preserves the existing preset and removes the incomplete write")
    with patch("yui.presets.os.replace", side_effect=OSError("simulated full disk")):
        reject(lambda: store.delete("My clip"), "failed delete reports a friendly error")
    check(store.path.read_bytes() == old_file and "My clip" in store.names(),
          "failed delete preserves the saved preset")
    store.delete("my clip")
    check("My clip" not in store.names() and "My clip" not in PresetStore(store.path).names(),
          "delete persists across restart")

    sanitized = replace(Settings(), fps=float("inf"), max_mb=500, background="#invalid",
                        priority=[], boomerang="true", image_seconds=-1)
    store.save_current("Recovered", sanitized)
    restored = store.get("Recovered")
    check(restored["fps"] == 0 and restored["max_mb"] == 10 and restored["background"] == "#000000"
          and restored["priority"] == "auto" and restored["boomerang"] is False
          and restored["image_seconds"] == .1,
          "sanitization repairs invalid fields and bounds finite values")
    check(sanitized.fps == float("inf"), "sanitization leaves the caller untouched")

    bad_path = fixtures / "damaged.json"
    for contents in (b"not json", b"\xff\xfe", b"[]", b'{"version":1,"presets":[]}',
                     b'{"version":2,"presets":{"Old":{}}}', b'{"version":true,"presets":{"Old":{}}}',
                     ("[" * 2000 + "]" * 2000).encode(), b" " * (MAX_FILE_BYTES + 1)):
        bad_path.write_bytes(contents)
        check(PresetStore(bad_path).names() == list(BUILTIN_NAMES), "damaged or unknown schema safely restores built-ins")
    bad_path.write_text(json.dumps({"version": 1, "presets": {
        "balanced": {"fps": 1}, "bad\nname": {}, "Invalid": [],
        "CUSTOM": {"fps": 7},
        "Good": {"fps": 19, "priority": [], "image_seconds": 5, "output_dir": "PRIVATE_FOLDER_MARKER"},
        "good": {"fps": 2}, "Partial": {"max_mb": 2},
    }}), encoding="utf-8")
    recovered = PresetStore(bad_path)
    check(recovered.names() == [*BUILTIN_NAMES, "Good", "Partial"],
          "loading skips built-ins, Custom status, invalid entries and case-insensitive collisions")
    check(recovered.get("Good")["fps"] == 19 and recovered.get("Good")["priority"] == "auto"
          and recovered.get("Partial")["speed"] == 1 and "output_dir" not in recovered.get("Good"),
          "valid fields survive damaged fields and omitted options inherit defaults")

    limits = PresetStore(fixtures / "limit.json")
    for number in range(MAX_SAVED_PRESETS):
        limits.save_current(f"Preset {number:02}", Settings(fps=number))
    reject(lambda: limits.save_current("Overflow", Settings()), "saved preset count is bounded")
    limits.save_current("Preset 00", Settings(fps=21), overwrite=True)
    check(len(limits.names()) == len(BUILTIN_NAMES) + MAX_SAVED_PRESETS
          and limits.get("Preset 00")["fps"] == 21, "replacement remains available at the preset limit")
    bad_path.write_text(json.dumps({"version": 1, "presets": {f"Preset {index}": {} for index in range(80)}}),
                        encoding="utf-8")
    check(len(PresetStore(bad_path).names()) == len(BUILTIN_NAMES) + MAX_SAVED_PRESETS,
          "oversized collections are bounded while loading")
    local = PresetStore()
    check(local.path == work / "profile" / "YuiGifConverter" / "presets.json",
          "default preset storage uses the isolated local app profile")
    reject(lambda: PresetStore(Path(r"\\invalid-share\no-account\presets.json")),
           "network profile is rejected before access")

    report = {"ok": True, "checks": checks, "count": len(checks)}
    (work / "presets-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
