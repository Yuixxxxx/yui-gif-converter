"""Verify cancellable video probes, thumbnails and close-time process cleanup."""

from pathlib import Path
import json
import os
import sys
import threading
import time
from unittest.mock import PropertyMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from yui import TERMS_VERSION, engine
from yui.settings import Settings
from yui.ui import App
from PIL.GifImagePlugin import GifImageFile


def main():
    media, work = map(Path, sys.argv[1:3])
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    with patch.object(GifImageFile, "n_frames", new_callable=PropertyMock,
                      side_effect=AssertionError("Classifying a GIF scanned every frame")):
        info = engine._probe_pil(media / "anim.gif", should_cancel=lambda: False)
        assert info[2] > 1
    for name in ("durationless_5s.webm", "timestamp_free_5s.mp4"):
        holder = {}
        item = engine.probe(media / name, should_cancel=lambda: False, proc_holder=holder)
        assert abs(item.duration - 5) <= 0.1 and holder["proc"] is None
        thumb = engine.make_thumbnail(item, should_cancel=lambda: False, proc_holder=holder)
        assert thumb.width > 0 and thumb.height > 0 and holder["proc"] is None

    def make_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.withdraw()
        return root

    started = threading.Event()
    ended = threading.Event()
    child = []
    actual_popen = engine.subprocess.Popen

    def spawn(*args, **kwargs):
        proc = actual_popen(*args, **kwargs)
        child.append(proc)
        started.set()
        return proc

    def slow_probe(path, **kwargs):
        try:
            return engine._run_capture(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                should_cancel=kwargs["should_cancel"], proc_holder=kwargs["proc_holder"],
            )
        finally:
            ended.set()

    with patch.object(App, "_make_root", make_root), \
         patch.object(Settings, "load", return_value=Settings(accepted_terms=TERMS_VERSION, sound=False)), \
         patch.object(engine, "probe", side_effect=slow_probe), \
         patch.object(engine.subprocess, "Popen", side_effect=spawn):
        app = App()
        fixture = work / "slow.mp4"
        fixture.write_bytes(b"fixture")
        app.add_paths([fixture])
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            app.root.update()
            time.sleep(0.005)
        assert started.is_set(), "Background probe did not start"
        before = time.monotonic()
        app._on_close()
        assert ended.wait(1), "Closing left the background capture running"
        elapsed = time.monotonic() - before
        assert all(proc.poll() is not None for proc in child), "A child process survived close"
        assert not app._background_procs

    report = {"ok": True, "cancellable_duration_probes_detect_5s": True,
              "gif_classification_avoids_full_frame_scan": True,
              "cancellable_thumbnail_capture": True, "closing_stops_background_children": True,
              "close_cleanup_seconds": round(elapsed, 3)}
    (work / "background-test.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
