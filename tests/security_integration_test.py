"""Exercise Windows security integrations with isolated fixtures.

Clipboard APIs are replaced with in-memory mocks. Native job tests constrain
only synthetic Python children; the app UI stays hidden and never contacts an
account, launches a browser, or changes the real clipboard.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import customtkinter as ctk
from yui import TERMS_VERSION, engine
from yui import windows
from yui.settings import Settings
from yui.ui import App, MAX_MEDIA_ITEMS


class ClipboardFixture:
    """Model ownership of movable global memory without using the clipboard."""
    def __init__(self, *, fail_set=None, fail_open=False, fail_allocate=None, fail_lock=None, fail_register=False):
        self.events = []
        self.buffers, self.handles, self.freed, self.transferred = {}, [], [], []
        self.format_ids = {"CanUploadToCloudClipboard": 0xC001, "CanIncludeInClipboardHistory": 0xC002}
        self.fail_set, self.fail_open = fail_set, fail_open
        self.fail_allocate, self.fail_lock, self.fail_register = fail_allocate, fail_lock, fail_register
        self.kernel = SimpleNamespace(
            GlobalAlloc=Mock(side_effect=self.allocate), GlobalLock=Mock(side_effect=self.lock),
            GlobalUnlock=Mock(side_effect=lambda handle: self.events.append(("unlock", handle)) or True),
            GlobalFree=Mock(side_effect=self.free),
        )
        self.user = SimpleNamespace(
            RegisterClipboardFormatW=Mock(side_effect=self.register),
            OpenClipboard=Mock(side_effect=self.open), EmptyClipboard=Mock(side_effect=self.empty),
            SetClipboardData=Mock(side_effect=self.set_data),
            CloseClipboard=Mock(side_effect=lambda: self.events.append(("close",)) or True),
        )

    def register(self, name):
        self.events.append(("register", name))
        return 0 if self.fail_register else self.format_ids[name]

    def allocate(self, flags, size):
        self.events.append(("allocate", flags, size))
        if len(self.handles) + 1 == self.fail_allocate:
            return 0
        handle = (1 << 36) + len(self.handles) + 1
        self.handles.append(handle)
        self.buffers[handle] = ctypes.create_string_buffer(size)
        return handle

    def lock(self, handle):
        self.events.append(("lock", handle))
        if self.handles.index(handle) + 1 == self.fail_lock:
            return 0
        return ctypes.addressof(self.buffers[handle])

    def free(self, handle):
        assert handle not in self.transferred, "Windows-owned memory was freed by the app"
        assert handle not in self.freed, "Global memory was freed twice"
        self.freed.append(handle)
        self.events.append(("free", handle))
        return 0

    def open(self, owner):
        self.events.append(("open", owner))
        return not self.fail_open

    def empty(self):
        self.events.append(("empty",))
        return True

    def set_data(self, format_id, handle):
        self.events.append(("set", format_id, handle))
        if format_id == self.fail_set:
            return 0
        self.transferred.append(handle)
        return handle

    def payload(self, handle):
        return bytes(self.buffers[handle])


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="security-integrations-", dir=work))
    os.environ["APPDATA"] = str(fixture / "profile")
    os.environ["LOCALAPPDATA"] = str(fixture / "local-profile")
    checks, limitations = [], []

    def check(condition, label):
        assert condition, label
        checks.append(label)

    gif = fixture / "apostrophe's-\u732b.gif"
    gif.write_bytes(b"GIF89a")
    owner = (1 << 36) + 99

    def clipboard_run(case, succeeds):
        with patch.object(windows.ctypes, "windll", SimpleNamespace(user32=case.user, kernel32=case.kernel)), \
             patch("yui.windows.is_local_path", return_value=True), \
             patch("subprocess.run", side_effect=AssertionError("Clipboard used a shell")):
            try:
                windows.copy_file(gif, owner)
            except OSError:
                assert not succeeds, "Expected clipboard transfer to succeed"
            else:
                assert succeeds, "Expected clipboard failure to abort transfer"
        return case

    if os.name == "nt":
        success = clipboard_run(ClipboardFixture(), True)
        published = [event for event in success.events if event[0] == "set"]
        check([event[1] for event in published] == [0xC001, 0xC002, 15],
              "cloud and history exclusions are published before the file")
        check(all(success.payload(event[2]) == struct.pack("<I", 0) for event in published[:2]),
              "both clipboard exclusion formats contain a zero DWORD")
        payload = success.payload(published[2][2])
        check(struct.unpack("<IiiII", payload[:20]) == (20, 0, 0, 0, 1),
              "CF_HDROP uses a Unicode DROPFILES header")
        check(payload[20:].decode("utf-16-le") == str(gif.absolute()) + "\0\0",
              "Unicode and apostrophe filenames survive the double-null terminated file list")
        check(success.freed == [] and success.transferred == success.handles,
              "successful transfer leaves all allocations owned by Windows")
        check(success.kernel.GlobalLock.restype is ctypes.c_void_p
              and success.kernel.GlobalAlloc.restype is wintypes.HGLOBAL
              and success.user.SetClipboardData.restype is wintypes.HANDLE
              and all(handle > 0xFFFFFFFF for handle in success.transferred)
              and success.user.OpenClipboard.call_args.args == (owner,),
              "64-bit pointers and owner handles are declared and preserved")
        check(all(ctypes.addressof(buffer) > 0xFFFFFFFF for buffer in success.buffers.values()),
              "clipboard copy exercises addresses above the 32-bit range")
        opened_at = next(index for index, event in enumerate(success.events) if event[0] == "open")
        check(all(index < opened_at for index, event in enumerate(success.events)
                  if event[0] in ("allocate", "lock", "unlock")),
              "allocation and filling finish before replacing clipboard contents")
        check(success.user.CloseClipboard.call_count == 1, "successful clipboard operation closes its session")

        for failed_format in (0xC001, 0xC002, 15):
            failed = clipboard_run(ClipboardFixture(fail_set=failed_format), False)
            set_events = [event for event in failed.events if event[0] == "set"]
            if failed_format != 15:
                check(not any(event[1] == 15 for event in set_events),
                      "failed exclusion publication never publishes the file")
            check(set(failed.freed) == set(failed.handles) - set(failed.transferred),
                  "failed clipboard publication frees only untransferred allocations")
            check(failed.user.CloseClipboard.call_count == 1, "failed publication closes its clipboard session")
        unopened = clipboard_run(ClipboardFixture(fail_open=True), False)
        check(unopened.user.EmptyClipboard.call_count == 0 and unopened.user.CloseClipboard.call_count == 0
              and unopened.freed == unopened.handles,
              "busy clipboard keeps previous contents and frees every unused allocation")
        unregistered = clipboard_run(ClipboardFixture(fail_register=True), False)
        check(unregistered.handles == [] and unregistered.user.OpenClipboard.call_count == 0,
              "unavailable exclusion formats abort before clipboard access")
        partial = clipboard_run(ClipboardFixture(fail_allocate=2), False)
        check(partial.freed == partial.handles and partial.user.OpenClipboard.call_count == 0,
              "partial allocation failure frees earlier memory without opening clipboard")
        unlocked = clipboard_run(ClipboardFixture(fail_lock=2), False)
        check(unlocked.freed == unlocked.handles and unlocked.user.OpenClipboard.call_count == 0,
              "lock failure frees all allocated memory before clipboard access")

        buffer = ctypes.create_unicode_buffer(32768)
        native_length = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
        assert native_length and native_length < len(buffer), "Native Windows folder lookup failed"
        native_windows = Path(buffer.value)
        lookup_calls = []
        def native_lookup(destination, length):
            lookup_calls.append(length)
            destination.value = str(native_windows)
            return len(str(native_windows))
        hostile = {"FAKE_API_KEY": "SYNTHETIC_CREDENTIAL", "OPENAI_API_KEY": "SYNTHETIC_CREDENTIAL",
                   "FFREPORT": "file=private-report", "IMAGEIO_FFMPEG_EXE": "untrusted-converter",
                   "PATH": "UNTRUSTED_SEARCH_FOLDER", "WINDIR": "UNTRUSTED_WINDOWS_FOLDER",
                   "SystemRoot": "UNTRUSTED_WINDOWS_FOLDER", "PYTHONPATH": "UNTRUSTED_PYTHON_FOLDER"}
        with patch.dict(os.environ, hostile), \
             patch.object(ctypes.windll.kernel32, "GetWindowsDirectoryW", side_effect=native_lookup):
            child_env = windows.child_environment()
            explorer = Path(windows.system_executable("explorer"))
            powershell = Path(windows.system_executable("powershell"))
        check(child_env["SystemRoot"] == str(native_windows) and child_env["WINDIR"] == str(native_windows)
              and child_env["PATH"] == str(native_windows / "System32") + ";" + str(native_windows),
              "decoder environment builds Windows paths from the native API")
        check(not any(key in child_env for key in ("FAKE_API_KEY", "OPENAI_API_KEY", "FFREPORT", "IMAGEIO_FFMPEG_EXE", "PYTHONPATH"))
              and not any("UNTRUSTED" in value or "SYNTHETIC_CREDENTIAL" in value for value in child_env.values()),
              "decoder environment excludes credentials, reporting and executable overrides")
        check(set(child_env) == {"SystemRoot", "WINDIR", "PATH", "TEMP", "TMP"}
              and child_env["TEMP"] == child_env["TMP"]
              and Path(child_env["TEMP"]).is_relative_to(fixture),
              "child environment contains only required Windows keys and isolated temporary storage")
        check(explorer == native_windows / "explorer.exe"
              and powershell == native_windows / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
              and explorer.is_absolute() and powershell.is_absolute() and len(lookup_calls) == 3,
              "system executables use absolute native Windows paths despite hostile environment values")
        try:
            windows.system_executable("untrusted")
        except ValueError:
            check(True, "only explicitly supported Windows integrations can be launched")
        else:
            raise AssertionError("Arbitrary system executable was accepted")

        memory_limit = 128 * 1024 * 1024
        executable = str(getattr(sys, "_base_executable", sys.executable))
        def child(code):
            return subprocess.Popen([executable, "-u", "-c", code], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=windows.child_environment(), creationflags=engine.CREATE_NO_WINDOW)
        allocation_code = ("import sys\nsys.stdin.readline()\ntry:\n data=bytearray(256*1024*1024)\n"
                           "except MemoryError:\n print('memory_denied',flush=True)\n"
                           "else:\n print('unexpected_allocation',flush=True)\n")
        process = child(allocation_code)
        guard = None
        try:
            guard = windows.ProcessGuard(process, memory_bytes=memory_limit)
            output, _ = process.communicate("allocate\n", timeout=15)
            check(process.returncode == 0 and output.strip() == "memory_denied",
                  "native 128 MiB job limit denies a synthetic 256 MiB allocation")
        finally:
            if guard is not None:
                guard.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        spawn_code = ("import subprocess,sys,time\nsys.stdin.readline()\ntry:\n"
                      " child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
                      "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                      "except OSError:\n print('child_denied',flush=True)\n"
                      "else:\n"
                      " deadline=time.monotonic()+3\n"
                      " while child.poll() is None and time.monotonic()<deadline: time.sleep(.02)\n"
                      " if child.poll() is not None: print('child_blocked',flush=True)\n"
                      " else:\n  child.kill();child.wait();print('unexpected_child',flush=True)\n")
        process = child(spawn_code)
        guard = None
        try:
            guard = windows.ProcessGuard(process, memory_bytes=memory_limit)
            output, _ = process.communicate("spawn\n", timeout=15)
            check(process.returncode == 0 and output.strip() in ("child_denied", "child_blocked"),
                  "native active-process limit prevents decoder child processes")
        finally:
            if guard is not None:
                guard.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

        process = child("import sys,time\nsys.stdin.readline()\ntime.sleep(60)\n")
        guard = None
        try:
            guard = windows.ProcessGuard(process, memory_bytes=memory_limit)
            check(process.poll() is None, "synthetic worker is alive before its guard closes")
            guard.close()
            process.wait(timeout=5)
            check(process.poll() is not None and guard.handle is None, "closing the native guard terminates its worker")
            guard.close()
            check(guard.handle is None, "native guard cleanup is idempotent")
        finally:
            if guard is not None:
                guard.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
    else:
        limitations.append("Native Windows integrations require Windows.")

    check(Settings().private_filenames is True, "anonymous output filenames are enabled by default")
    preferences = Settings(accepted_terms=TERMS_VERSION, sound=False, output_dir=str(fixture / "outputs"),
                           theme="light", mode="separate")
    errors = []
    def hidden_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.withdraw()
        return root
    def hidden_dialog(app, title, width, height):
        top = ctk.CTkToplevel(app.root)
        top.withdraw()
        top.title(title)
        top.geometry(f"{width}x{height}")
        return top
    with patch.object(App, "_make_root", hidden_root), patch.object(App, "_dialog", hidden_dialog), \
         patch.object(Settings, "load", return_value=preferences):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        try:
            app.root.update_idletasks()
            before = asdict(app.settings)
            app.open_settings()
            switches = [widget for widget in app._walk(app._settings_window)
                        if isinstance(widget, ctk.CTkSwitch) and widget.cget("text") == "Use anonymous GIF filenames"]
            check(len(switches) == 1 and switches[0].get() == 1,
                  "Settings exposes an enabled anonymous filename control")
            switches[0].toggle()
            check(app.settings.private_filenames is False
                  and {key: value for key, value in asdict(app.settings).items() if key != "private_filenames"}
                  == {key: value for key, value in before.items() if key != "private_filenames"},
                  "privacy toggle changes only the filename preference")
            app.presets.save_current("Integration", app.settings)
            app._apply_preset("Small file")
            app._apply_preset("Integration")
            check(app.settings.private_filenames is False and app.settings.mode == before["mode"]
                  and app.settings.output_dir == before["output_dir"] and app.settings.theme == before["theme"]
                  and app.settings.accepted_terms == before["accepted_terms"]
                  and "private_filenames" not in app.presets.get("Integration"),
                  "preset application preserves filename privacy and workflow choices")
            switches[0].toggle()
            check(app.settings.private_filenames is True, "anonymous filename preference can be restored")
            items = [engine.MediaItem(fixture / "SOURCE_PRIVATE_SENTINEL.mp4", "video", 64, 64, duration=2, fps=10)]
            app.items = items
            with patch.object(app, "_show_view"), patch.object(app, "_rebuild_rows"), \
                 patch("yui.ui.threading.Thread") as thread:
                app._start()
                jobs = thread.call_args.kwargs["args"][0]
            check(len(jobs) == 1 and re.fullmatch(r"yui-[0-9a-f]{12}\.gif", jobs[0][1].name)
                  and "SOURCE_PRIVATE_SENTINEL" not in jobs[0][1].name,
                  "default conversion targets omit the source filename")
            app.busy = False

            original = engine.MediaItem(fixture / "original.mp4", "video", 64, 64, duration=2, fps=10)
            first = engine.MediaItem(fixture / "new.mp4", "video", 64, 64, duration=2, fps=10)
            duplicate = engine.MediaItem(fixture / "NEW.mp4", "video", 64, 64, duration=2, fps=10)
            second = engine.MediaItem(fixture / "other" / "new.mp4", "video", 64, 64, duration=2, fps=10)
            app.items = [original]
            status = []
            with patch.object(app, "_show_view"), patch.object(app, "_rebuild_rows"), \
                 patch.object(app, "_set_status", side_effect=lambda *args, **kwargs: status.append(args[0])), \
                 patch.object(app.pool, "submit") as submit:
                app._apply_import([original, first, duplicate, second], [], 0)
                check(app.items == [original, first, second] and submit.call_count == 2,
                      "duplicate imports are skipped case-insensitively and unique items retain order")
                check("Skipped 2" in status[-1], "duplicate import status reports skipped items")
                app.items = [engine.MediaItem(fixture / f"item-{index}.mp4", "video", 64, 64, duration=1, fps=10)
                             for index in range(MAX_MEDIA_ITEMS - 1)]
                additions = [engine.MediaItem(fixture / f"extra-{index}.mp4", "video", 64, 64, duration=1, fps=10)
                             for index in range(6)]
                submit.reset_mock()
                app._apply_import(additions, [], 0)
                check(MAX_MEDIA_ITEMS == 200 and len(app.items) == 200 and app.items[-1] is additions[0]
                      and submit.call_count == 1, "media list is capped at 200 without scheduling excess thumbnails")
                check("Skipped 5" in status[-1] and "limit 200" in status[-1],
                      "media cap reports excess imports clearly")
            check(not errors, "hidden app integration produces no Tk callback failures")
        finally:
            app._on_close()

    report = {"ok": True, "count": len(checks), "checks": checks, "limitations": limitations,
              "real_clipboard_modified": False, "accounts_accessed": False,
              "memory_limit_mib": 128, "synthetic_requested_allocation_mib": 256}
    (work / "security-integration-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
