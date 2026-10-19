"""Exercise responsiveness, row edits and an explicit-only support link."""

from pathlib import Path
import json
import os
import queue
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from yui import PROFILE_URL, SUPPORT_URL, TERMS_VERSION, engine
from yui.settings import Settings
from yui.ui import App, SoftButton


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    errors = []
    opened = []

    def make_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.withdraw()
        return root

    def browser(url, **kwargs):
        opened.append(url)
        return True

    with patch.object(App, "_make_root", make_root), \
         patch.object(Settings, "load", return_value=Settings(accepted_terms=TERMS_VERSION, sound=False)), \
         patch("webbrowser.open", side_effect=browser):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(str(args[1]))

        def spin(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                app.root.update()
                time.sleep(0.002)

        try:
            spin(0.12)
            assert not opened, "Support website opened without a click"
            first = engine.MediaItem(work / "first.mp4", "video", 480, 720, duration=5, fps=25)
            second = engine.MediaItem(work / "second.mp4", "video", 480, 720, duration=5, fps=25)
            app.items = [first, second]
            app._rebuild_rows()
            original = app.rows[0]
            original.start_entry.delete(0, "end")
            original.start_entry.insert(0, "1.25")
            app.move_item(first, 1)
            assert app.rows[1] is original
            assert original.start_entry.get() == "1.25", "Reordering lost an unfinished trim edit"
            assert original.number_label.cget("text") == "2"
            assert original.move_buttons[1].cget("state") == "disabled"
            original._commit_trim()
            assert first.trim_start == 1.25 and first.trim_end == 5
            app.remove_item(second)
            assert app.rows == [original]
            assert all(button.cget("state") == "disabled" for button in original.move_buttons)

            # Every callback runs immediately; its visual pulse never changes geometry.
            called = []
            button = SoftButton(app.root, text="Test", width=110, height=32,
                                command=lambda: called.append(time.monotonic()))
            app.root._yui_motion_enabled = lambda: True
            for theme in ("dark", "light"):
                ctk.set_appearance_mode(theme)
                before = time.monotonic()
                button.invoke()
                assert called[-1] - before < 0.05
                assert button.cget("width") == 110 and button.cget("height") == 32
                assert button._pulse_job is not None
                spin(0.20)
                assert button._pulse_job is None
            button.configure(state="disabled")
            count = len(called)
            button.invoke()
            assert len(called) == count and button._pulse_job is None
            button.configure(state="normal", command=lambda: button.configure(text="Cancel", fg_color="#333333"))
            button.invoke()
            assert button._pulse_job is None and button.cget("fg_color") == "#333333"
            app.root._yui_motion_enabled = lambda: False
            button.configure(command=lambda: called.append(time.monotonic()))
            button.invoke()
            assert button._pulse_job is None
            button.destroy()

            # Progress updates interpolate without holding the event loop.
            app.busy = True
            app._system_animations = True
            app._ui_progress(0.8, "Working")
            spin(0.06)
            assert 0 < app.progress.get() < 0.8
            spin(0.14)
            assert abs(app.progress.get() - 0.8) < 0.01
            app._ui_finished([], [], True)
            assert app._progress_job is None

            # Folder scanning and media probing stay off the Tk thread and keep order.
            folder = work / "batch"
            folder.mkdir(exist_ok=True)
            for name in ("b.mp4", "a.mp4"):
                (folder / name).write_bytes(b"fixture")
            main_thread = threading.get_ident()
            actual_iterdir = Path.iterdir
            worker_threads = []

            def list_files(path):
                if path == folder:
                    assert threading.get_ident() != main_thread, "Folder scan blocked the UI thread"
                return actual_iterdir(path)

            def read(path, **kwargs):
                worker_threads.append(threading.get_ident())
                time.sleep(0.03)
                return engine.MediaItem(path, "video", 480, 720, duration=5, fps=25)

            with patch.object(Path, "iterdir", list_files), patch.object(engine, "probe", side_effect=read), \
                 patch.object(engine, "make_thumbnail", return_value=None):
                app.add_paths([folder])
                deadline = time.monotonic() + 2
                while app.loading and time.monotonic() < deadline:
                    spin(0.015)
                assert app.loading == 0
            assert worker_threads and all(value != main_thread for value in worker_threads)
            assert [item.path.name for item in app.items[-2:]] == ["a.mp4", "b.mp4"]
            app.settings.max_mb = 2
            app._reset_options()
            assert app.settings.max_mb == 10

            assert not opened
            app.support_button.invoke()
            deadline = time.monotonic() + 1
            while not opened and time.monotonic() < deadline:
                spin(0.01)
            assert opened == [SUPPORT_URL] and SUPPORT_URL == "https://ko-fi.com/yui_xxx"
            assert app.profile_button.grid_info()["row"] > app.support_button.grid_info()["row"]
            assert app.profile_button.cget("width") == app.support_button.cget("width") == 170
            assert app.profile_button.cget("height") == app.support_button.cget("height") == 36
            assert app._profile_image is not None
            app.profile_button.invoke()
            deadline = time.monotonic() + 1
            while len(opened) < 2 and time.monotonic() < deadline:
                spin(.01)
            assert opened == [SUPPORT_URL, PROFILE_URL]
            assert PROFILE_URL == "https://www.juicychat.ai/userdetailspace/1933409514837725186"
            assert not errors, errors
            report = {"ok": True, "unfinished_trim_preserved_on_reorder": True,
                      "button_feedback_does_not_delay_actions": True,
                      "disabled_and_reduced_motion_passed": True, "smooth_progress_passed": True,
                      "background_folder_scan_and_probe": True, "input_order_preserved": True,
                      "reset_options_passed": True, "support_opens_only_on_click": True,
                      "support_url": SUPPORT_URL, "inspection": "isolated hidden Tk runtime"}
            report.update(profile_below_support=True, matching_button_dimensions=True,
                          offline_avatar_loaded=True, profile_opens_only_on_click=True,
                          profile_url=PROFILE_URL)
            (work / "interaction-test.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report, indent=2))
        finally:
            app._on_close()


if __name__ == "__main__":
    main()
