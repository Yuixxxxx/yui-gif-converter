"""Synthetic frame archive, lossless split and native media tools checks."""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image, ImageDraw, PngImagePlugin
from yui import engine, media_tools
from yui.overlays import OverlayLayer
from yui.paths import get_ffmpeg, temp_dir


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local")
    checks = []

    def check(value, name):
        assert value, name
        checks.append(name)

    def rejects(function, name, error=engine.MediaError):
        try:
            function()
        except error:
            checks.append(name)
        else:
            raise AssertionError(name)

    still = work / "private-source.png"
    canary = "PRIVATE_CREATOR_AND_CAMERA_CANARY"
    image = Image.new("RGB", (160, 100), "red")
    ImageDraw.Draw(image).rectangle((80, 0, 159, 99), fill="blue")
    info = PngImagePlugin.PngInfo()
    info.add_text("Author", canary)
    info.add_text("FilePath", "C:/private/account/source.png")
    image.save(still, pnginfo=info)
    sticker = work / "private-sticker.png"
    Image.new("RGBA", (30, 30), "#ff00ff").save(sticker, pnginfo=info)
    animation = work / "private-animation.png"
    Image.new("RGBA", (160, 100), "red").save(animation, save_all=True,
        append_images=[Image.new("RGBA", (160, 100), "blue")], duration=[1000, 1000], loop=0)
    video = work / "private-movie.mp4"
    subprocess.run([get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=160x100:r=20:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=160x100:r=20:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video)], check=True)
    originals = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (still, sticker, animation, video)}
    photo, movie, animated = engine.probe(still), engine.probe(video), engine.probe(animation)
    movie.start, movie.end = .25, 1.75
    movie.thumb = object()
    left, right = media_tools.split_item(movie, .6)
    check(abs(left.start - .25) < 1e-8 and abs(left.end - .85) < 1e-8 and abs(right.start - .85) < 1e-8 and abs(right.end - 1.75) < 1e-8,
          "split uses a relative trim time and preserves exact absolute boundaries")
    check(left.path == right.path == movie.path and left.duration == right.duration == movie.duration and left.thumb is movie.thumb and right.thumb is movie.thumb,
          "split preserves original media, duration, decoder and thumbnail")
    check(movie.start == .25 and movie.end == 1.75 and left is not movie and right is not movie,
          "split never mutates the imported source model")
    a, b = media_tools.split_item(animated, 1)
    check(a.clip_seconds == b.clip_seconds == 1, "Pillow animation split retains both halves in order")
    rejects(lambda: media_tools.split_item(photo, 1), "stills cannot be split")
    for value in (0, .01, 1.49, 2, float("nan"), float("inf"), True, "1", 10**1000):
        rejects(lambda value=value: media_tools.split_item(movie, value), f"invalid split time rejected {str(value)[:18]}")
    a, b = media_tools.split_item(movie, .1)
    check(abs(a.clip_seconds - .1) < 1e-9 and abs(b.clip_seconds - 1.4) < 1e-9, "minimum 0.1 second split remains valid")
    opts = engine.RenderOptions(max_side=320)

    def read_archive(path):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            check(all(entry.filename == f"frame{index+1:04d}.png" and entry.date_time == (1980, 1, 1, 0, 0, 0) and entry.create_system == 0 for index, entry in enumerate(entries)),
                  f"{path.name} contains anonymous names and constant timestamps")
            frames = []
            for entry in entries:
                payload = archive.read(entry)
                check(canary.encode() not in payload and b"private/account" not in payload, "frame excludes source identity strings")
                with Image.open(io.BytesIO(payload)) as frame:
                    check(frame.info == {} and frame.mode == "RGB", "frame is clean RGB without EXIF ICC or PNG text")
                    frames.append(frame.copy())
            return frames

    progress = []
    archive = media_tools.extract_frames(movie, opts, work / "movie.zip", fps=4, max_side=320,
        progress=lambda value, message: progress.append((value, threading.get_ident())))
    frames = read_archive(archive)
    check(len(frames) == 6 and frames[0].size == (320, 200), "trimmed video samples six bounded PNG frames with correct aspect")
    check(frames[0].getpixel((160, 100))[0] > 200 and frames[-1].getpixel((160, 100))[2] > 200,
          "video frame archive preserves trim range and temporal order")
    check(progress and progress[0][0] == 0 and progress[-1][0] == 1 and all(0 <= value <= 1 for value, _ in progress),
          "extraction reports bounded progress and completion")
    backwards = read_archive(media_tools.extract_frames(movie, replace(opts, reverse=True), work / "reverse.zip", fps=4, max_side=320))
    check(backwards[0].tobytes() == frames[-1].tobytes() and backwards[-1].tobytes() == frames[0].tobytes(),
          "reverse reorders selected video frames exactly once")
    animated_frames = read_archive(media_tools.extract_frames(animated, opts, work / "animation.zip", fps=2, max_side=320))
    check(len(animated_frames) == 4 and animated_frames[0].getpixel((10, 10)) == (255, 0, 0) and animated_frames[-1].getpixel((10, 10)) == (0, 0, 255),
          "Pillow animation extraction uses source frame delays and one traversal")
    edited = replace(opts, crop=(.5, 0, .5, 1), effect="invert", overlays=(OverlayLayer(kind="image", image=sticker, x=.5, y=.5, size=.2),))
    edited_frames = read_archive(media_tools.extract_frames(photo, edited, work / "edited.zip", fps=.5, max_side=320))
    check(len(edited_frames) == 1 and edited_frames[0].size == (256, 320), "crop determines extraction aspect without GIF size planner shrinkage")
    check(edited_frames[0].getpixel((5, 5)) == (255, 255, 0), "extracted image uses shared crop and effect")
    check(edited_frames[0].getpixel((128, 160)) == (255, 0, 255), "static watermark is above transformed extracted source")
    all_gray = read_archive(media_tools.extract_frames(movie, replace(opts, effect="grayscale"), work / "gray.zip", fps=1, max_side=320))
    gray = all_gray[0].getpixel((20, 20))
    check(max(gray) - min(gray) < 3, "video extraction uses shared colour effect filter")
    check(media_tools.frame_range(photo, start=.1, end=.2, fps=.5)[3] == 1, "sub-frame extraction interval keeps one frame")

    sentinel = work / "existing.zip"
    sentinel.write_bytes(b"keep archive")
    unique = media_tools.extract_frames(photo, opts, sentinel, fps=.5, max_side=320)
    check(unique.name == "existing (2).zip" and sentinel.read_bytes() == b"keep archive", "existing ZIPs are kept with numbered publication")
    for kwargs in (dict(start=-1), dict(start=1, end=.5), dict(end=100), dict(fps=0), dict(fps=31), dict(fps=True), dict(fps=float("nan")), dict(max_side=319), dict(max_side=1921), dict(max_side=True)):
        rejects(lambda kwargs=kwargs: media_tools.extract_frames(photo, opts, work / "invalid.zip", **kwargs), f"invalid extraction request rejected {kwargs}")
    rejects(lambda: media_tools.extract_frames(replace(photo, hold=600), opts, work / "too-many.zip", fps=1), "frame count limit checked before decode", engine.MediaLimitError)
    rejects(lambda: media_tools.extract_frames(photo, replace(opts, effect="movie=secrets"), work / "unsafe.zip"), "unrecognized filter expression rejected")
    rejects(lambda: media_tools.extract_frames(photo, opts, work / "bad.png"), "archive extension is explicit")
    rejects(lambda: media_tools.extract_frames(photo, opts, Path(r"\\example.invalid\share\frames.zip")), "network output rejected before save")
    rejects(lambda: media_tools.extract_frames(replace(photo, path=Path(r"\\example.invalid\share\source.png")), opts, work / "network.zip"), "network source rejected before decode")
    rejects(lambda: media_tools.extract_frames(photo, opts, sentinel, protected_paths=(sentinel,)), "imported media alias is never overwritten")
    protected_sticker = work / "sticker-alias.zip"
    protected_sticker.write_bytes(b"existing protected data")
    rejects(lambda: media_tools.extract_frames(photo, opts, protected_sticker, protected_paths=(protected_sticker,)), "other imported source is protected")
    before = set(temp_dir().iterdir())
    rejects(lambda: media_tools.extract_frames(photo, opts, work / "cancelled.zip", should_cancel=lambda: True), "early cancellation publishes nothing", engine.RenderCancelled)
    cancellation = threading.Event()
    rejects(lambda: media_tools.extract_frames(movie, opts, work / "cancelled-late.zip", fps=4,
        should_cancel=cancellation.is_set, progress=lambda value, message: cancellation.set() if value >= .65 else None),
        "mid-extraction cancellation removes staged frames", engine.RenderCancelled)
    with patch.object(media_tools, "MAX_ARCHIVE_BYTES", 100):
        rejects(lambda: media_tools.extract_frames(photo, opts, work / "too-large.zip", fps=.5), "disk cap aborts before archive publication", engine.MediaLimitError)
    check(not (work / "cancelled.zip").exists() and not (work / "cancelled-late.zip").exists() and not (work / "too-large.zip").exists() and set(temp_dir().iterdir()) == before,
          "cancel and disk limit leave no output or private temporary folder")
    check(not list(work.glob(".yui-save-*")), "atomic archive publication leaves no staging files")
    check(all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in originals.items()), "all original images videos animations and stickers are byte-identical")

    # Native widgets plus real previews. Kept after backend checks so headless
    # source checks can explicitly select --no-ui without implying GUI evidence.
    native = "--no-ui" not in sys.argv
    if native:
        import customtkinter as ctk
        from yui import toolbox
        applied, split_events, closed = [], [], []
        root = ctk.CTk()
        root.title("Yui media tools test · synthetic media only")
        root.geometry("900x700+20+20")
        root.update()

        def pump(predicate, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                if predicate():
                    return
                time.sleep(.015)
            raise AssertionError("native tools event loop timed out")

        window = toolbox.Toolbox(root, items=(photo, movie), options=opts, on_apply=applied.append,
            on_split=lambda index, parts: split_events.append((index, parts)), on_close=lambda: closed.append(True))
        pump(lambda: window._rendered_key is not None)
        check(window.grab_current() is window, "media tools is locally modal")
        check(window._image.size == (640, 400) and window.apply_button.cget("state") == "normal", "native toolbox renders a real source preview and enables Apply")
        window.crop_vars[0].set("99")
        pump(lambda: "inside" in window.status.cget("text"))
        check(window.apply_button.cget("state") == "disabled", "invalid crop leaves Apply disabled and reports validation")
        window.reset_crop()
        window.effect_var.set("Sepia")
        window._draft_changed()
        pump(lambda: window._rendered_key is not None and window._rendered_key[1] == "sepia")
        check(not applied, "preview edits remain a draft before explicit Apply")
        with patch.object(toolbox, "ask_confirmation", return_value=False):
            check(window.close() is False and window.winfo_exists(), "discard decline keeps native editor and unsaved draft open")
        window.apply()
        check(len(applied) == 1 and applied[0].effect == "sepia" and closed == [True] and window._closed, "Apply commits edit fields exactly once and closes cleanly")
        window = toolbox.Toolbox(root, items=(movie,), options=opts, on_apply=applied.append,
            on_split=lambda *args: (_ for _ in ()).throw(engine.MediaError("Import limit reached.")))
        pump(lambda: window._rendered_key is not None)
        window.effect_var.set("Sepia")
        window.split_var.set("0.5")
        window.split()
        check(len(applied) == 1 and not window._closed and "Import limit" in window.status.cget("text"),
              "rejected split keeps draft edits unapplied and the toolbox open")
        window.close(force=True)
        window = toolbox.Toolbox(root, items=(photo, movie), options=opts, on_apply=applied.append,
            on_split=lambda index, parts: split_events.append((index, parts)))
        window.source_var.set(window.source_names[1])
        window._source_changed()
        pump(lambda: window._rendered_key is not None)
        check(window._selected == 1 and window.split_button.cget("state") == "normal", "source picker enables split only for the selected moving clip")
        window.split_var.set("0.5")
        window.split()
        check(window._closed and len(split_events) == 1 and split_events[0][0] == 1 and len(split_events[0][1]) == 2, "native split callback receives the selected index and both clips")
        window = toolbox.Toolbox(root, items=(photo,), options=opts, on_apply=applied.append, on_split=lambda *args: None)
        pump(lambda: window._rendered_key is not None)
        window.fps_var.set("0.5")
        window.extract(work / "ui-extracted.zip")
        pump(lambda: not window._exporting)
        check((work / "ui-extracted.zip").exists() and "Saved frames" in window.status.cget("text"), "native extraction saves a real ZIP asynchronously and returns completion")
        window.close(force=True)
        check(window.close(force=True) is True and root.grab_current() is None, "toolbox forced closure is idempotent and releases grab")
        for scale in (1, 1.5, 2):
            ctk.set_widget_scaling(scale)
            ctk.set_window_scaling(scale)
            root.update()
            window = toolbox.Toolbox(root, items=(photo,), options=opts, on_apply=applied.append, on_split=lambda *args: None)
            pump(lambda: window._rendered_key is not None)
            root.update_idletasks()
            width, height = window.winfo_width(), window.winfo_height()
            prefix = f"{scale:g}x scaling: "
            check(width <= window.winfo_screenwidth() - 58 and height <= window.winfo_screenheight() - 98,
                  prefix + "initial native geometry stays inside display margins")
            for label, widget in (("footer", window.footer), ("Close", window.close_button), ("Apply", window.apply_button),
                                  ("controls", window.controls._parent_frame), ("source picker", window.source_menu),
                                  ("preview canvas", window.canvas)):
                x = widget.winfo_rootx() - window.winfo_rootx()
                y = widget.winfo_rooty() - window.winfo_rooty()
                check(widget.winfo_ismapped() and x >= 0 and y >= 0 and x + widget.winfo_width() <= width + 1 and y + widget.winfo_height() <= height + 1,
                      prefix + label + " stays inside the native client area")
            check(int(window.canvas.cget("width")) == 1 and window.canvas.winfo_width() > 60 and window.canvas.winfo_height() > 20,
                  prefix + "preview canvas expands into available space without a fixed width request")
            check(window.source_menu.cget("dynamic_resizing") is False,
                  prefix + "long source names cannot force the preview column wider")
            window.close(force=True)
        # Exercise the same native widgets against a smaller physical display
        # budget even when the test machine itself has a large monitor.
        with patch.object(toolbox.Toolbox, "winfo_screenwidth", return_value=1366), patch.object(toolbox.Toolbox, "winfo_screenheight", return_value=768):
            window = toolbox.Toolbox(root, items=(photo,), options=opts, on_apply=applied.append, on_split=lambda *args: None)
        pump(lambda: window._rendered_key is not None)
        root.update_idletasks()
        check(window.winfo_width() <= 1308 and window.winfo_height() <= 670,
              "2x scaling on 1366x768 budget keeps window within physical display margins")
        for label, widget in (("Close", window.close_button), ("Apply", window.apply_button), ("controls", window.controls._parent_frame), ("canvas", window.canvas)):
            x = widget.winfo_rootx() - window.winfo_rootx()
            y = widget.winfo_rooty() - window.winfo_rooty()
            check(x >= 0 and y >= 0 and x + widget.winfo_width() <= window.winfo_width() + 1 and y + widget.winfo_height() <= window.winfo_height() + 1,
                  "2x scaling on 1366x768 budget: " + label + " fits native client area")
        window.close(force=True)
        ctk.set_widget_scaling(1)
        ctk.set_window_scaling(1)
        root.destroy()
    report = dict(ok=True, passed=len(checks), native_ui=native, checks=checks)
    (work / "media-tools-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
