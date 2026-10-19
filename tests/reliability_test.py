"""Reproduce settings, draft edits, import races and safe output failure cases.

All media/profile files are synthetic and isolated below the supplied work folder.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import customtkinter as ctk
from PIL import Image
from yui import TERMS_VERSION, engine
from yui.settings import Settings
from yui.ui import App


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    checks = []

    def check(condition, name):
        assert condition, name
        checks.append(name)

    for payload in (
        {"fps": float("inf"), "image_seconds": float("nan"), "theme": "light"},
        {"priority": [], "background": "#zzzzzz", "output_dir": [], "theme": "light"},
        {"open_when_done": "false", "overwrite": "false", "boomerang": "true", "theme": "light"},
    ):
        Settings.path().write_text(json.dumps(payload))
        settings = Settings.load()
        check(settings.theme == "light", "valid settings survive a damaged field")
        check(settings.background == "#000000" and isinstance(settings.output_dir, str), "invalid colour and path recover")
        check(settings.fps == 0 and settings.image_seconds == 2, "nonfinite numbers recover")
        check(not settings.open_when_done and not settings.overwrite and not settings.boomerang, "string booleans do not enable actions")
    Settings.path().write_bytes(b"\xff\xfe\x00garbage")
    check(Settings.load() == Settings(), "damaged encoding recovers on startup")
    old = b'{"theme":"light"}'
    Settings.path().write_bytes(old)
    previous_temporaries = set(Settings.path().parent.glob(".settings-*.tmp"))
    with patch("yui.settings.os.replace", side_effect=OSError("simulated storage failure")):
        Settings(theme="dark").save()
    check(Settings.path().read_bytes() == old and set(Settings.path().parent.glob(".settings-*.tmp")) == previous_temporaries,
          "failed settings write preserves previous preferences and removes temporary files")
    Settings(theme="light", image_seconds=3).save()
    check(Settings.load().theme == "light" and Settings.load().image_seconds == 3, "settings save and reload")
    check(all(engine.parse_time(value) is None for value in ("nan", "inf", "1e309", "1:inf", "1e308:1e308")),
          "nonfinite timestamps rejected")
    check(engine.parse_time("1:05.25") == 65.25, "normal timestamp parsing")

    def make_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.withdraw()
        return root

    errors = []
    release = threading.Event()
    started = threading.Event()
    ended = threading.Event()
    with patch.object(App, "_make_root", make_root), \
         patch.object(Settings, "load", return_value=Settings(accepted_terms=TERMS_VERSION, sound=False)):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        def spin(seconds):
            until = time.monotonic() + seconds
            while time.monotonic() < until:
                app.root.update()
                time.sleep(.002)
        try:
            spin(.12)
            item = engine.MediaItem(work / "clip.mp4", "video", 480, 720, duration=5, fps=25)
            app.items = [item]
            app._rebuild_rows()
            row = app.rows[0]
            for entry, value in ((row.start_entry, "1.25"), (row.end_entry, "4")):
                entry.delete(0, "end"); entry.insert(0, value)
            captured = []
            with patch.object(app, "_start", side_effect=lambda: captured.append((item.trim_start, item.trim_end))):
                app.on_primary()
            check(captured == [(1.25, 4)], "Create commits unfinished trim before rendering")
            still_item = engine.MediaItem(work / "hold.png", "image", 64, 64, hold=2)
            app.items = [still_item]; app._rebuild_rows()
            entry = app.rows[0].hold_entry
            entry.delete(0, "end"); entry.insert(0, "3.5")
            with patch.object(app, "_start", side_effect=lambda: captured.append(still_item.hold)):
                app.on_primary()
            check(captured[-1] == 3.5, "Create commits unfinished image duration")
            app.settings.output_dir = str(work / "new-output-folder")
            jobs = []
            with patch("yui.ui.threading.Thread") as thread:
                app._start()
                jobs.extend(thread.call_args.kwargs["args"][0])
            still_item.hold = 9
            check(jobs[0][0][0].hold == 3.5 and jobs[0][0][0] is not still_item,
                  "active conversion has an independent media snapshot")
            check(jobs[0][1].parent == Path(app.settings.output_dir) and jobs[0][1].parent.is_dir(),
                  "missing chosen output folder is created without silently changing location")
            app._ui_finished([], [], True)
            app._shown_result = object()
            app.clear_items()
            check(app._shown_result is None, "clear removes stale result actions")

            fixture = work / "pending.mp4"; fixture.write_bytes(b"fixture")
            def delayed(path, **kwargs):
                started.set()
                release.wait(2)
                ended.set()
                return engine.MediaItem(path, "video", 320, 180, duration=5, fps=25)
            with patch.object(engine, "probe", side_effect=delayed), \
                 patch.object(engine, "make_thumbnail", return_value=None):
                app.add_paths([fixture])
                check(started.wait(1), "import starts in background")
                check(app.primary.cget("text") == "Cancel import", "import cancellation is available")
                app.clear_items(); release.set()
                check(ended.wait(1), "cancelled import worker finishes")
                spin(.2)
                check(not app.items and app.loading == 0, "late import cannot repopulate a cleared list")
                release.clear(); started.clear(); ended.clear()
                app.add_paths([fixture]); check(started.wait(1), "second import starts")
                app.on_primary(); release.set(); ended.wait(1); spin(.2)
                check(not app.items and app.loading == 0, "Cancel import discards stale completion")

            first, second = work / "slow-first.mp4", work / "fast-second.mp4"
            def out_of_order(path, **kwargs):
                time.sleep(.12 if path == first else .01)
                return engine.MediaItem(path, "video", 320, 180, duration=5, fps=25)
            with patch.object(engine, "probe", side_effect=out_of_order), \
                 patch.object(engine, "make_thumbnail", return_value=None):
                app.add_paths([first]); app.add_paths([second])
                until = time.monotonic() + 2
                while app.loading and time.monotonic() < until: spin(.02)
            check([it.path for it in app.items] == [first, second], "separate imports retain user order despite different probe speeds")
            app.open_settings(); window = app._settings_window
            app.open_settings()
            check(app._settings_window is window, "repeated Settings click reuses one dialog")
            window.destroy()
            check(not errors, "no Tk callback errors")
        finally:
            release.set()
            app._on_close()

    still = work / "still.png"; Image.new("RGB", (64, 64), "pink").save(still)
    target = work / "existing.gif"; target.write_bytes(b"existing output")
    source = engine.probe(still, hold=.2)
    with patch.object(engine.shutil, "copyfile", side_effect=OSError("simulated disk failure")):
        try:
            engine.render([source], engine.RenderOptions(), target, overwrite=True)
            raise AssertionError("Save failure was ignored")
        except engine.MediaError: pass
    check(target.read_bytes() == b"existing output" and not list(work.glob(".yui-save-*.tmp")),
          "failed save preserves existing GIF and cleans temporary file")
    saved = engine.render([source], engine.RenderOptions(), target)
    check(saved.output != target and target.read_bytes() == b"existing output", "replacement off retains existing output")
    replacement = engine.render([source], engine.RenderOptions(), target, overwrite=True)
    with Image.open(replacement.output) as gif:
        check(gif.format == "GIF", "replacement on publishes a complete readable GIF")
    collision = work / "race.gif"
    def progress(frac, message):
        if frac >= .02 and not collision.exists(): collision.write_bytes(b"another job won")
    raced = engine.render([source], engine.RenderOptions(), collision, progress=progress)
    check(raced.output != collision and collision.read_bytes() == b"another job won", "late output collision cannot overwrite another job")
    original = still.read_bytes()
    try:
        engine.render([source], engine.RenderOptions(), still, overwrite=True)
        raise AssertionError("Input overwrite was allowed")
    except engine.MediaError: pass
    check(still.read_bytes() == original, "input files cannot be replaced")
    cancelled = work / "cancelled.gif"
    try:
        engine._publish_output(target, cancelled, False, lambda: True)
        raise AssertionError("Save cancellation was ignored")
    except engine.RenderCancelled: pass
    check(not cancelled.exists() and not list(work.glob(".yui-save-*.tmp")), "cancel before publication leaves no partial output")
    not_folder = work / "not-a-folder"; not_folder.write_bytes(b"fixture")
    try:
        engine.output_path(still, not_folder)
        raise AssertionError("Unavailable chosen folder silently changed")
    except engine.MediaError: pass
    checks.append("unavailable chosen folder reports an actionable error")
    broken = work / "broken.mp4"; broken.write_bytes(b"not media")
    try:
        engine.probe(broken)
        raise AssertionError("Corrupt media was accepted")
    except engine.MediaError: pass
    checks.append("corrupt media reports a recoverable error")
    report = {"ok": True, "checks": checks, "check_count": len(checks),
              "inspection": "synthetic media and isolated hidden Tk runtime"}
    (work / "reliability-test.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
