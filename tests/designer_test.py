"""Exercise the actual Tk editor, rendering workers, history and protected exports."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from PIL import Image, ImageDraw
from yui import engine, overlays
from yui.designer import Designer, MAX_LAYERS


def main():
    work = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "work" / "designer-check").resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    image = Image.new("RGB", (400, 300), "#173350")
    ImageDraw.Draw(image).rectangle((0, 240, 400, 300), fill="#468b61")
    source = work / "poster source.png"
    image.save(source)
    sticker = work / "sticker {space}.png"
    bitmap = Image.new("RGBA", (100, 100))
    ImageDraw.Draw(bitmap).ellipse((5, 5, 95, 95), fill="#fcda61")
    bitmap.save(sticker)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    sticker_hash = hashlib.sha256(sticker.read_bytes()).hexdigest()
    main_thread = threading.get_ident()
    worker_threads = []
    callback_errors = []
    checks = []
    applied, closed = [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    root = ctk.CTk()
    root.withdraw()
    root.report_callback_exception = lambda *args: callback_errors.append(str(args[1]))
    editor = None

    def spin(seconds=.12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.003)

    def until(predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("Timed out waiting for editor: " + (editor.status.cget("text") if editor else "unknown"))
            spin(.015)

    def ready():
        return not editor._loading and editor._rendered_layers == editor.layers

    actual_preview, actual_compose, actual_render, actual_export = (
        overlays.preview_frame, overlays.compose_overlay, overlays.render_layer, overlays.export_poster)

    def background(function):
        def invoke(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            check_thread = threading.get_ident() != main_thread
            if not check_thread:
                raise AssertionError("Decode / compose / export ran on the Tk thread")
            return function(*args, **kwargs)
        return invoke

    with patch.object(overlays, "preview_frame", side_effect=background(actual_preview)), \
         patch.object(overlays, "compose_overlay", side_effect=background(actual_compose)), \
         patch.object(overlays, "render_layer", side_effect=background(actual_render)), \
         patch.object(overlays, "export_poster", side_effect=background(actual_export)):
        try:
            item = engine.MediaItem(source, "image", 400, 300)
            editor = Designer(root, source=item, options=engine.RenderOptions(), canvas_size=(400, 300),
                              on_apply=lambda layers: applied.append(layers), on_close=lambda: closed.append(True))
            # Map a real native window outside the visible desktop, so Tk gives
            # real layout coordinates while no user websites/accounts are touched.
            editor.geometry("1060x760+12000+12000")
            until(ready)
            check("native editor opens and renders source", editor._rendered.size == (400, 300))
            check("poster-only source disables time slider", editor.frame_slider.cget("state") == "disabled")
            check("empty design has no delete/undo", editor.delete_button.cget("state") == "disabled" and not editor._undo)
            check("preview preserves source frame colors", editor._rendered.getpixel((0, 0))[:3] == (23, 51, 80))

            editor.add_text()
            until(ready)
            check("text adds selected layer with visible controls", len(editor.layers) == 1 and editor._selected == 0 and editor.text_properties.winfo_ismapped())
            editor.text_box.delete("1.0", "end")
            editor.text_box.insert("1.0", "Summer\n夜空")
            editor.text_box.event_generate("<KeyRelease>")
            spin()
            until(ready)
            check("editable multiline Unicode text", editor.layers[0].text == "Summer\n夜空")
            editor._property_changed("color", "#ff2266")
            editor._slider_changed("size", 18)
            editor._slider_changed("rotation", 20)
            editor._slider_changed("opacity", 70)
            editor._property_changed("font", "Georgia")
            until(ready)
            check("text styles affect current layer", editor.layers[0].color == "#ff2266" and editor.layers[0].size == .18 and editor.layers[0].rotation == 20 and editor.layers[0].opacity == .7 and editor.layers[0].font == "Georgia")
            check("composed preview differs from source", editor._rendered.tobytes() != image.convert("RGBA").tobytes())

            editor.position_entries["x"].delete(0, "end")
            editor.position_entries["x"].insert(0, "75")
            editor._commit_position("x")
            check("numeric placement is a dragging alternative", editor.layers[0].x == .75)
            editor.position_entries["y"].delete(0, "end")
            editor.position_entries["y"].insert(0, "nan")
            editor._commit_position("y")
            check("invalid position is rejected without corrupting draft", editor.layers[0].y == .25 and "between 0 and 100" in editor.status.cget("text"))
            editor.center()
            until(ready)
            check("center button centers both axes", editor.layers[0].x == editor.layers[0].y == .5)

            # Native Canvas bindings use fitted-image coordinates, not the
            # window's extra margins; repeat after changing the window size.
            for width, height in ((1060, 760), (940, 620)):
                editor.geometry(f"{width}x{height}+12000+12000")
                spin(.18)
                left, top, view_width, view_height = editor._view
                center_x, center_y = round(left + view_width * .5), round(top + view_height * .5)
                before = editor.layers[0]
                history_before = len(editor._undo)
                editor.canvas.event_generate("<Button-1>", x=center_x, y=center_y)
                editor.canvas.event_generate("<B1-Motion>", x=center_x + round(view_width * .1), y=center_y + round(view_height * .1))
                editor.canvas.event_generate("<B1-Motion>", x=center_x + round(view_width * .2), y=center_y + round(view_height * .2))
                editor.canvas.event_generate("<ButtonRelease-1>", x=center_x, y=center_y)
                spin(.1)
                until(ready)
                check(f"fitted canvas drag moves correct normalized position at {width}", abs(editor.layers[0].x - .7) < .01 and abs(editor.layers[0].y - .7) < .01)
                check(f"drag gesture is one undo step at {width}", len(editor._undo) == history_before + 1)
                editor.undo()
                check(f"undo restores exact drag start at {width}", editor.layers[0] == before)
                editor.redo()
                check(f"redo restores dragged position at {width}", abs(editor.layers[0].x - .7) < .01)
                editor.center()
                until(ready)

            before = editor.layers[0]
            editor.nudge(1, 0)
            check("arrow nudge equals one output pixel", abs(editor.layers[0].x - before.x - 1 / 400) < 1e-8)
            editor.nudge(0, 1, fine=False)
            check("shift arrow nudge equals ten output pixels", abs(editor.layers[0].y - before.y - 10 / 300) < 1e-8)

            editor.add_image(sticker)
            until(lambda: len(editor.layers) == 2 and ready())
            check("sticker is decoded off Tk and selected", editor.layers[1].kind == "image" and editor.layers[1].image == sticker and not editor.text_properties.winfo_ismapped())
            check("new sticker starts in front", editor._selected == 1)
            editor.reorder(-1)
            check("layer order moves sticker behind text", editor.layers[0].kind == "image" and editor._selected == 0)
            editor.reorder(1)
            editor.duplicate()
            until(ready)
            check("duplicate preserves style and offsets placement", len(editor.layers) == 3 and editor.layers[2].image == sticker and editor.layers[2].x == .53)
            editor.delete()
            check("delete keeps remaining layers", len(editor.layers) == 2)
            # No stale rendered bounds can select an index deleted from the draft.
            editor.canvas.event_generate("<Button-1>", x=100, y=100)
            spin(.08)
            check("stale preview clicks never index deleted layers", not callback_errors)
            editor.undo()
            check("undo deletion restores copied layer", len(editor.layers) == 3)
            editor.redo()
            until(ready)

            # Poster export must be an explicit operation and must preserve
            # all input bytes; PNG contains no source metadata or account info.
            output = work / "Designed poster.png"
            output.unlink(missing_ok=True)
            editor.save_poster(output)
            until(lambda: not editor._exporting)
            with Image.open(output) as poster:
                check("explicit poster export reaches chosen resolution", poster.size == (1600, 1200))
                check("poster output has no copied metadata", not poster.info)
                check("poster contains image sticker on source frame", poster.getpixel((800, 600))[:3] == (252, 218, 97))
            check("saving poster does not apply design", not applied)
            check("source and sticker bytes remain unchanged", hashlib.sha256(source.read_bytes()).hexdigest() == source_hash and hashlib.sha256(sticker.read_bytes()).hexdigest() == sticker_hash)
            editor.save_poster(source, overwrite=True)
            until(lambda: not editor._exporting)
            check("poster cannot replace source", "cannot replace" in editor.status.cget("text").lower() and hashlib.sha256(source.read_bytes()).hexdigest() == source_hash)
            with patch("yui.designer.filedialog.asksaveasfilename", return_value=r"\\unavailable-server\share\poster.png"), \
                 patch.object(Path, "exists", side_effect=AssertionError("UNC was statted before rejection")):
                editor.save_poster()
            check("network destination rejected before stat or confirmation", "local drive" in editor.status.cget("text"))

            with patch("yui.designer.ask_confirmation", return_value=False):
                result = editor.close()
            check("discard confirmation can keep editor open", result is False and not editor._closed and not closed)
            expected = editor.layers
            editor.apply()
            check("apply returns immutable layers once and closes", applied == [expected] and closed == [True] and editor._closed)
            check("close is idempotent", editor.close(force=True) is True and closed == [True])
            check("all decode and composition ran on background threads", worker_threads and main_thread not in worker_threads)

            # Seek rapidly through a synthetic video. The older slow request
            # must not replace the latest selected frame, even after it finishes.
            video = engine.MediaItem(work / "synthetic.mp4", "video", 400, 300, duration=5, fps=25)
            offsets = []
            def synthetic_preview(item, opts, canvas_size, *, offset=0, should_cancel=None, proc_holder=None):
                offsets.append(offset)
                time.sleep(.16 if offset == 1 else .02)
                return Image.new("RGBA", canvas_size, (int(offset * 40), 40, 80, 255))
            with patch.object(overlays, "preview_frame", side_effect=background(synthetic_preview)):
                editor = Designer(root, source=video, options=engine.RenderOptions(), canvas_size=(400, 300), on_apply=lambda layers: None)
                editor.geometry("1060x760+12000+12000")
                until(ready)
                editor._change_frame(1)
                check("seeking immediately disables poster until frame arrives", editor._loading and editor.export_button.cget("state") == "disabled")
                spin(.16)
                editor._change_frame(3)
                until(lambda: ready() and editor._rendered.getpixel((0, 0))[0] == 120)
                spin(.25)
                check("latest frame wins asynchronous seek", editor._offset == 3 and editor._rendered.getpixel((0, 0))[0] == 120)
                editor.add_text()
                editor._change_frame(4)
                editor.close(force=True)
                spin(.25)
                check("close cancels outstanding jobs and disconnects poll", editor._closed and not editor._jobs and not callback_errors)

            # Full layer limit and total-text validation cannot silently apply
            # an invalid draft, and a render failure must gate Apply / export.
            editor = Designer(root, source=item, options=engine.RenderOptions(), canvas_size=(400, 300), on_apply=lambda layers: applied.append(layers))
            editor.geometry("1060x760+12000+12000")
            until(ready)
            for _ in range(MAX_LAYERS + 1):
                editor.add_text()
            check("layer count capped with disabled add controls", len(editor.layers) == MAX_LAYERS and editor.add_text_button.cget("state") == "disabled")
            until(ready)
            editor.select(0)
            editor._property_changed("text", "a" * 2100)
            check("oversize text is visibly truncated to supported draft", len(editor.layers[0].text) == 2000 and len(editor.text_box.get("1.0", "end-1c")) == 2000)
            until(ready)
            with patch.object(overlays, "compose_overlay", side_effect=engine.MediaError("Fixture render limit")):
                editor._property_changed("size", .11)
                until(lambda: "Fixture render limit" in editor.status.cget("text"))
                check("unrenderable drafts cannot apply or export", editor.apply_button.cget("state") == "disabled" and editor.export_button.cget("state") == "disabled")
                before_applied = len(applied)
                editor.apply()
                check("failed render cannot call apply callback", len(applied) == before_applied)
            editor.close(force=True)
            spin(.1)
            check("no Tk callback exceptions across editing and teardown", not callback_errors)
        finally:
            if editor and not editor._closed:
                editor.close(force=True)
            spin(.1)
            root.destroy()
    report = {"ok": True, "checks": checks, "count": len(checks),
              "native_tk": True, "real_image_decode_and_poster_export": True,
              "synthetic_video_seek": True, "user_accounts_touched": False}
    (work / "designer-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
