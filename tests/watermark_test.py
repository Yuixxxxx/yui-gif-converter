"""Native editor watermark shortcuts, placement, history and safe local images."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from PIL import Image, ImageDraw
from yui import engine, overlays
from yui.designer import ANCHOR_CHOICES, Designer, MAX_LAYERS


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "work" / "watermark-check").resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local")
    source = work / "source.png"
    Image.new("RGB", (400, 300), "#112b40").save(source)
    sticker = work / "logo with spaces.png"
    bitmap = Image.new("RGBA", (160, 80))
    ImageDraw.Draw(bitmap).rectangle((12, 10, 148, 70), fill="#ff00ff")
    bitmap.save(sticker)
    original = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (source, sticker)}
    item = engine.MediaItem(source, "image", 400, 300)
    checks, callback_errors, applied = [], [], []
    root = ctk.CTk()
    root.withdraw()
    root.report_callback_exception = lambda *args: callback_errors.append(str(args[1]))
    editor = None
    main_thread = threading.get_ident()
    decode_threads = []
    actual_render = overlays.render_layer

    def render_layer(*args, **kwargs):
        decode_threads.append(threading.get_ident())
        if threading.get_ident() == main_thread:
            raise AssertionError("Watermark decoding ran on the Tk event thread")
        return actual_render(*args, **kwargs)

    def check(name, condition):
        assert condition, name
        checks.append(name)

    def spin(seconds=.03):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.003)

    def until(predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate():
            assert time.monotonic() < deadline, "Watermark check timed out: " + editor.status.cget("text")
            spin()

    def ready():
        return not editor._loading and editor._rendered_layers == editor.layers

    def open_editor(mode="Dark"):
        ctk.set_appearance_mode(mode)
        result = Designer(root, source=item, options=engine.RenderOptions(), canvas_size=(400, 300),
                          on_apply=lambda layers: applied.append(layers))
        result.geometry("1060x760+12000+12000")
        return result

    with patch.object(overlays, "render_layer", side_effect=render_layer):
        try:
            editor = open_editor()
            until(ready)
            check("watermark shortcuts are explicit and enabled", editor.text_watermark_button.cget("text") == "Text watermark"
                  and editor.image_watermark_button.cget("text") == "Image watermark"
                  and editor.text_watermark_button.cget("state") == "normal")
            check("anchor presets are disabled without a selection", editor.anchor_menu.cget("state") == "disabled")
            editor.add_watermark()
            until(ready)
            mark = editor.layers[0]
            check("text watermark has private-data-free editable branding", mark.kind == "text" and mark.text == "Yui")
            check("text watermark defaults to a subtle outlined corner label", (mark.x, mark.y, mark.size, mark.opacity, mark.outline)
                  == (.82, .9, .06, .45, True))
            editor.undo()
            check("watermark addition is one undo step", editor.layers == ())
            editor.redo()
            until(ready)
            check("redo restores exact watermark style", editor.layers == (mark,))
            editor.text_box.delete("1.0", "end")
            editor.text_box.insert("1.0", "My artwork")
            until(lambda: editor.layers[0].text == "My artwork" and ready())
            check("watermark text uses the normal text editor", editor.layers[0].opacity == .45)

            for anchor in ANCHOR_CHOICES:
                before, steps = editor.layers, len(editor._undo)
                editor.set_anchor(anchor)
                until(ready)
                positioned = editor.layers
                bounds = editor._bounds[0]
                check(f"{anchor} keeps the full watermark on canvas", bounds[0] >= 0 and bounds[1] >= 0
                      and bounds[2] <= 400 and bounds[3] <= 300)
                if anchor == "Center":
                    check("center anchor aligns both axes", editor.layers[0].x == editor.layers[0].y == .5)
                else:
                    check(f"{anchor} has the expected corner", (editor.layers[0].x < .5) == anchor.endswith("left")
                          and (editor.layers[0].y < .5) == anchor.startswith("Top"))
                if before != positioned:
                    check(f"{anchor} is a single undoable movement", len(editor._undo) == steps + 1)
                    editor.undo()
                    check(f"undo restores {anchor} movement exactly", editor.layers == before)
                    editor.redo()
                    until(ready)
                    check(f"redo restores {anchor} movement exactly", editor.layers == positioned)
            editor.nudge(1, 0)
            check("manual movement remains available after anchoring", editor.anchor_menu.get() == "Custom position")
            editor.center()
            check("existing Center button remains available", editor.layers[0].x == editor.layers[0].y == .5)

            editor.add_watermark("image", sticker)
            check("image watermark loads asynchronously with loading feedback", editor._pending_stickers == 1
                  and editor.image_watermark_button.cget("state") == "disabled")
            # A normal text edit while decoding must never alter the queued
            # image's own watermark style or position.
            editor._property_changed("opacity", .8)
            until(lambda: len(editor.layers) == 2 and ready())
            logo = editor.layers[1]
            check("image watermark preserves its independent queued defaults", logo.kind == "image" and logo.image == sticker
                  and logo.size == .18 and logo.opacity == .45 and logo.x > .5 and logo.y > .5)
            check("image watermark has a safe bottom-right margin", editor._bounds[1][2] <= 388
                  and editor._bounds[1][3] <= 291)
            check("image controls recover after asynchronous decoding", not editor._pending_stickers
                  and editor.image_watermark_button.cget("state") == "normal")
            editor._slider_changed("rotation", 35)
            until(ready)
            editor.set_anchor("Top left")
            until(ready)
            check("anchor uses actual rotated image bounds", min(editor._bounds[1][:2]) >= 0)
            editor._slider_changed("opacity", 20)
            until(ready)
            check("normal opacity control edits watermark opacity", editor.layers[1].opacity == .2)
            editor.undo()
            editor.redo()
            until(ready)
            check("normal watermark styling participates in history", editor.layers[1].opacity == .2)

            editor.add_text()
            until(ready)
            check("ordinary text defaults are unchanged", editor.layers[-1].text == "Your text"
                  and (editor.layers[-1].x, editor.layers[-1].y, editor.layers[-1].size, editor.layers[-1].opacity)
                  == (.5, .25, .1, 1))
            editor.add_image(sticker)
            until(lambda: len(editor.layers) == 4 and ready())
            check("ordinary sticker defaults are unchanged", (editor.layers[-1].x, editor.layers[-1].y,
                  editor.layers[-1].size, editor.layers[-1].opacity) == (.5, .5, .35, 1))
            before, history = editor.layers, len(editor._undo)
            with patch("yui.designer.filedialog.askopenfilename", return_value=""):
                editor.add_watermark("image")
            check("cancelling image selection leaves the draft and history unchanged", editor.layers == before
                  and len(editor._undo) == history and editor._pending_stickers == 0)
            editor.add_watermark("unsupported")
            check("unsupported watermark kinds cannot add a layer", editor.layers == before)
            editor.add_watermark("image", work / "missing.png")
            until(lambda: editor._pending_stickers == 0)
            check("failed image loading cannot change a draft", editor.layers == before
                  and editor.image_watermark_button.cget("state") == "normal")
            editor.add_watermark("image", Path(r"\\unavailable-server\share\logo.png"))
            until(lambda: editor._pending_stickers == 0)
            check("network image watermark is rejected", editor.layers == before
                  and "local drive" in editor.status.cget("text"))

            poster = work / "watermarked-poster.png"
            poster.unlink(missing_ok=True)
            editor.save_poster(poster)
            until(lambda: not editor._exporting)
            with Image.open(poster) as output:
                check("watermarks use existing metadata-free poster export", output.size == (1600, 1200) and not output.info)
            check("poster export does not silently apply a draft", not applied)
            editor.apply()
            check("Apply returns normal immutable layers including watermarks", applied == [before] and editor._closed)

            editor = open_editor("Light")
            until(ready)
            editor.add_watermark()
            until(ready)
            check("watermark UI supports the existing light theme", editor._get_appearance_mode() == "light"
                  and editor.anchor_menu.cget("dropdown_fg_color") == editor.theme["card"])
            for _ in range(MAX_LAYERS - 2):
                editor.add_text()
            until(ready)
            editor.add_watermark("image", sticker)
            editor.add_watermark()
            until(lambda: editor._pending_stickers == 0 and ready())
            check("a queued image cannot exceed a newly filled layer limit", len(editor.layers) == MAX_LAYERS)
            editor.add_watermark()
            editor.add_watermark("image", sticker)
            check("both watermark shortcuts respect the shared layer limit", len(editor.layers) == MAX_LAYERS
                  and editor.text_watermark_button.cget("state") == "disabled"
                  and editor.image_watermark_button.cget("state") == "disabled")
            editor.close(force=True)

            editor = open_editor()
            until(ready)
            started, release = threading.Event(), threading.Event()
            def slow_render(layer, canvas_size):
                if layer.kind == "image":
                    started.set()
                    release.wait(5)
                return render_layer(layer, canvas_size)
            with patch.object(overlays, "render_layer", side_effect=slow_render):
                editor.add_watermark("image", sticker)
                until(started.is_set)
                editor.close(force=True)
                release.set()
                spin(.15)
            check("closing cancels queued watermark addition and polling", editor._closed and not editor.layers and not editor._jobs)
            check("image decoding stays off the Tk event thread", decode_threads and main_thread not in decode_threads)
            check("original source and logo bytes are unchanged", all(hashlib.sha256(path.read_bytes()).hexdigest() == digest
                  for path, digest in original.items()))
            check("watermark editing and cancellation produce no Tk callback errors", not callback_errors)
        finally:
            if editor is not None and not editor._closed:
                editor.close(force=True)
            spin(.05)
            root.destroy()
            ctk.set_appearance_mode("System")
    report = {"ok": True, "checks": checks, "count": len(checks), "native_tk": True,
              "real_local_image_decode_and_poster_export": True, "user_accounts_touched": False}
    (work / "watermark-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
