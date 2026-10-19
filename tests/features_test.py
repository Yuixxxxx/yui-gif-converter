"""Exercise presets, Save As and playback controls together in an isolated app."""
import json
import os
import sys
import threading
import time
import uuid
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
    os.environ["APPDATA"] = str(work / ("profile-" + uuid.uuid4().hex))
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    def root(app):
        app.dnd_files = None
        result = ctk.CTk()
        result.withdraw()
        return result
    checks = []
    errors = []
    with patch.object(App, "_make_root", root), patch.object(Settings, "load", return_value=Settings(
        accepted_terms=TERMS_VERSION, sound=False, output_dir=str(work / "outputs"), theme="light")):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        def spin(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                app.root.update(); time.sleep(.002)
        def check(value, name):
            assert value, name
            checks.append(name)
        try:
            spin(.12)
            clip = engine.MediaItem(work / "clip.mp4", "video", 320, 180, duration=5, fps=25, start=1, end=4)
            photo = engine.MediaItem(work / "photo.png", "image", 64, 64, hold=3)
            app.items = [clip, photo]; app._rebuild_rows()
            before = (app.settings.theme, app.settings.output_dir, app.settings.accepted_terms, app.settings.sound)
            app._apply_preset("Small file")
            check(app.settings.max_mb == 3 and app.settings.max_side == 480 and app.preset_picker.get() == "Small file",
                  "built-in preset synchronizes conversion options")
            check(before == (app.settings.theme, app.settings.output_dir, app.settings.accepted_terms, app.settings.sound)
                  and (clip.trim_start, clip.trim_end, photo.hold) == (1, 4, 3),
                  "presets keep workflow preferences and existing trim/duration edits")
            app.settings.speed = 1.25; app._settings_changed()
            check(app.preset_picker.get() == "Custom", "manual option edits show Custom")
            with patch.object(ctk, "CTkInputDialog") as dialog:
                dialog.return_value.get_input.return_value = "Favorite"
                app._save_preset()
            check(app.preset_picker.get() == "Favorite" and app.delete_preset_button.cget("state") == "normal",
                  "UI saves a reusable local preset")
            app._apply_preset("Balanced"); app._apply_preset("Favorite")
            check(app.settings.speed == 1.25 and app.settings.max_mb == 3, "saved preset reapplies its options")
            app._delete_preset()
            check("Favorite" not in app.presets.names() and app.settings.speed == 1.25,
                  "deleting saved preset retains current options")
            long_name = "W" * 64
            app.presets.save_current(long_name, app.settings, overwrite=True)
            app._apply_preset(long_name); app.root.update_idletasks()
            check(app.preset_picker.winfo_reqwidth() <= 220, "long saved name cannot stretch options panel")
            app.settings.fps = 23
            for sync in app._option_widgets: sync()
            check(any(isinstance(widget, ctk.CTkOptionMenu) and widget.get() == "23 fps"
                      for widget in app._walk(app.options_panel)), "custom numeric option displays exact value")

            gif = work / "source.gif"
            frames = [Image.new("RGB", (80, 60), colour) for colour in ("red", "green", "blue")]
            frames[0].save(gif, save_all=True, append_images=frames[1:], duration=[120, 80, 160], loop=0)
            original = gif.read_bytes()
            result = engine.RenderResult(gif, gif.stat().st_size, 80, 60, 10, .36, 1, engine.HARD_CAP_BYTES)
            app.results = [result]; app._show_results([result])
            end = time.monotonic() + 1
            while not app.player.playable and time.monotonic() < end: spin(.01)
            spin(.04)
            check(app.pause_button.cget("state") == "normal", "animated GIF enables preview controls")
            app.toggle_preview(); image = app.preview_label.cget("image"); spin(.4)
            check(app.player.paused and app.pause_button.cget("text") == "Resume"
                  and image is app.preview_label.cget("image"), "Pause freezes frame and exposes Resume")
            app.toggle_preview(); check(not app.player.paused, "Resume continues preview")
            app.toggle_preview(); app.restart_preview()
            check(not app.player.paused, "Restart resumes from beginning")
            spin(.1)
            target = work / "renamed.gif"
            with patch("tkinter.filedialog.asksaveasfilename", return_value=str(target)):
                app.save_result_as()
            end = time.monotonic() + 2
            while app._save_copy_busy and time.monotonic() < end: spin(.01)
            check(target.read_bytes() == original and gif.read_bytes() == original,
                  "Save As preserves source and every encoded GIF byte")
            check(app._shown_result.output == target and app.results[0].output == target,
                  "result actions follow renamed output")
            with patch("tkinter.filedialog.asksaveasfilename", return_value=""):
                app.save_result_as()
            check(not app._save_copy_busy and app._shown_result.output == target, "cancelled file dialog keeps result")
            with patch("tkinter.filedialog.asksaveasfilename", return_value=str(photo.path)), \
                 patch.object(engine, "save_copy", side_effect=AssertionError("Input overwrite attempted")):
                app.save_result_as()
            check(not photo.path.exists() and not app._save_copy_busy, "Save As rejects input filename before writing")
            failed = work / "failed.gif"
            with patch("tkinter.filedialog.asksaveasfilename", return_value=str(failed)), \
                 patch.object(engine, "save_copy", side_effect=engine.MediaError("Synthetic failure")):
                app.save_result_as()
                end = time.monotonic() + 1
                while app._save_copy_busy and time.monotonic() < end: spin(.01)
            check(not failed.exists() and app.save_as_button.cget("state") == "normal"
                  and app._shown_result.output == target, "Save As failure recovers without losing result")

            # Long duplicate names must remain individually selectable.
            same1 = engine.RenderResult(work / "a" / ("long_" * 12 + "same.gif"), 100, 80, 60, 10, 1, 1, engine.HARD_CAP_BYTES)
            same2 = engine.RenderResult(work / "b" / same1.output.name, 100, 80, 60, 10, 1, 1, engine.HARD_CAP_BYTES)
            app.results = [same1, same2]
            with patch.object(app, "_preview") as preview:
                app._show_results(app.results)
                values = app.result_picker.cget("values")
                app._on_pick_result(values[1])
                check(len(set(values)) == 2 and preview.call_args.args[0] is same2,
                      "batch result picker distinguishes identical filenames")
                app._shown_result = same2
                renamed = work / "batch-copy.gif"
                app._save_as_done(same2, renamed, None)
                current = app._result_labels[app.result_picker.get()]
                check(current.output == renamed and preview.call_args.args[0].output == renamed,
                      "Save As keeps selected batch result aligned with picker")
                app._shown_result = current
                before_calls = preview.call_count
                first_copy = work / "first-copy.gif"
                app._save_as_done(same1, first_copy, None)
                check(app._result_labels[app.result_picker.cget("values")[0]].output == first_copy
                      and preview.call_count == before_calls,
                      "saving a background batch result refreshes labels without restarting selected preview")
            check(not errors, "no Tk callback errors during feature workflow")
            report = {"ok": True, "check_count": len(checks), "checks": checks,
                      "inspection": "isolated hidden Tk with real GIF IO and mocked file/name dialogs"}
            (work / "features-test.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            app._on_close()

if __name__ == "__main__": main()
