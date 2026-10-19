"""Yui Gif Converter — videos and images to GIFs that always stay at or under 10 MB.

Run:  python app.py [files…]
Build the .exe:  powershell -ExecutionPolicy Bypass -File build.ps1
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from pathlib import Path


def _ensure_console_streams() -> None:
    # Windowed executables have no console. Libraries may still emit a warning
    # (for example when Windows cannot load a font); keep their fallback usable.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _windows_setup() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Yui.GifConverter")
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _close_splash() -> None:
    try:
        import pyi_splash  # only exists inside the PyInstaller build

        pyi_splash.close()
    except Exception:
        pass


def _selftest(out_dir: Path) -> int:
    """Check bundled rendering, including a video without duration metadata."""
    import subprocess

    from PIL import Image, ImageDraw

    from yui import engine
    from yui.paths import get_ffmpeg, is_local_path
    from yui.presets import PresetStore
    from yui.settings import Settings

    if not is_local_path(out_dir):
        return 1
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(out_dir / "profile")
    os.environ["LOCALAPPDATA"] = str(out_dir / "local-profile")
    report = out_dir / "selftest.json"
    try:
        clip, still = out_dir / "selftest_clip.mp4", out_dir / "selftest_still.png"
        subprocess.run(
            [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=s=640x360:r=25:d=3", "-pix_fmt", "yuv420p", str(clip)],
            check=True, capture_output=True, creationflags=engine.CREATE_NO_WINDOW,
        )
        Image.new("RGB", (400, 400), (255, 107, 157)).save(still)
        items = [engine.probe(clip), engine.probe(still, hold=1.0)]
        result = engine.render(items, engine.RenderOptions(transition="fade"), out_dir / "selftest.gif")
        unknown = out_dir / "selftest_durationless.webm"
        subprocess.run(
            [get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "testsrc2=s=480x720:r=25:d=5", "-an", "-c:v", "libvpx-vp9",
             "-deadline", "realtime", "-cpu-used", "8", "-live", "1", str(unknown)],
            check=True, capture_output=True, creationflags=engine.CREATE_NO_WINDOW,
        )
        unknown_item = engine.probe(unknown)
        unknown_result = engine.render([unknown_item], engine.RenderOptions(), out_dir / "selftest_durationless.gif")
        with Image.open(unknown_result.output) as gif:
            actual_seconds = 0.0
            for i in range(gif.n_frames):
                gif.seek(i)
                actual_seconds += gif.info.get("duration", 0) / 1000
        duration_ok = abs(unknown_item.duration - 5.0) <= 0.1 and abs(actual_seconds - 5.0) <= 0.15
        from yui.overlays import OverlayLayer, export_poster
        sticker = out_dir / "selftest-sticker.png"
        mark = Image.new("RGBA", (64, 64))
        ImageDraw.Draw(mark).ellipse((2, 2, 61, 61), fill="#ff00ff")
        mark.save(sticker)
        design_layers = (OverlayLayer(text="Yui poster", x=.5, y=.16, size=.09),
                         OverlayLayer(kind="image", image=sticker, x=.75, y=.78, size=.25))
        design_result = engine.render([unknown_item], engine.RenderOptions(
            max_side=320, fps=12, overlays=design_layers), out_dir / "selftest-designed.gif")
        with Image.open(design_result.output) as designed:
            design_seconds = 0.0
            design_frames_ok = True
            for index in range(designed.n_frames):
                designed.seek(index)
                design_seconds += designed.info.get("duration", 0) / 1000
                pixel = designed.convert("RGB").getpixel((round(designed.width * .75), round(designed.height * .78)))
                design_frames_ok = design_frames_ok and pixel[0] > 225 and pixel[1] < 45 and pixel[2] > 225
        poster = export_poster(unknown_item, engine.RenderOptions(), design_layers, out_dir / "selftest-poster.png",
                              canvas_size=(480, 720), max_side=480)
        with Image.open(poster) as image:
            poster_pixel = image.getpixel((round(image.width * .75), round(image.height * .78)))
            poster_ok = image.format == "PNG" and image.size == (320, 480) and not image.info and poster_pixel == (255, 0, 255)
        design_ok = design_frames_ok and abs(design_seconds - 5.0) < .2 and poster_ok
        from yui import media_tools
        from zipfile import ZipFile
        from dataclasses import replace
        timeline = out_dir / "selftest-timeline.gif"
        red, blue = Image.new("RGB", (160, 120), "red"), Image.new("RGB", (160, 120), "blue")
        red.save(timeline, save_all=True, append_images=[blue], duration=1000, loop=0)
        timeline_item = engine.probe(timeline)
        watermarks = (OverlayLayer(text="Yui", x=.8, y=.9, size=.08, opacity=.45),)
        edits = engine.RenderOptions(max_side=160, fps=10, crop=(.25, 0, .5, 1), effect="grayscale",
                                     reverse=True, palette_colors=32, dither="none", overlays=watermarks)
        edited_result = engine.render([timeline_item], edits, out_dir / "selftest-edited.gif")
        with Image.open(edited_result.output) as edited:
            pixels = []
            palette_ok = True
            for index in range(edited.n_frames):
                edited.seek(index)
                rgb = edited.convert("RGB")
                pixels.append(rgb.getpixel((rgb.width // 2, rgb.height // 2)))
                palette_ok = palette_ok and rgb.getcolors(33) is not None
            editing_ok = (edited.width < edited.height and pixels[0][0] < pixels[-1][0] - 20
                          and all(max(p) - min(p) <= 3 for p in pixels) and palette_ok
                          and edited_result.size <= edited_result.cap_bytes)
        split_parts = media_tools.split_item(timeline_item, .75)
        split_ok = abs(split_parts[0].clip_seconds - .75) < .001 and abs(split_parts[1].clip_seconds - 1.25) < .001
        frame_zip = media_tools.extract_frames(timeline_item, edits, out_dir / "selftest-frames.zip", fps=2, max_side=320)
        with ZipFile(frame_zip) as archive:
            import io
            extraction_ok = archive.namelist() == [f"frame{i:04d}.png" for i in range(1, 5)]
            for name in archive.namelist():
                with Image.open(io.BytesIO(archive.read(name))) as frame:
                    extraction_ok = extraction_ok and frame.size == (213, 320) and not frame.info
        remote = out_dir / "selftest_remote.m3u8"
        remote.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttp://127.0.0.1:9/blocked.ts\n#EXT-X-ENDLIST\n")
        blocked = engine._run_capture([get_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error",
                                       "-i", str(remote), "-f", "null", "-"])
        network_ok = blocked.returncode != 0 and "not on whitelist" in (blocked.stderr or "").lower()
        copied = engine.save_copy(unknown_result.output, out_dir / "selftest-copy.gif")
        copy_ok = copied.read_bytes() == unknown_result.output.read_bytes()
        presets = PresetStore(out_dir / "selftest-presets.json")
        presets.save_current("Selftest", Settings(max_mb=3, fps=15), overwrite=True)
        loaded = PresetStore(out_dir / "selftest-presets.json")
        options = loaded.apply(Settings(theme="light"), "Selftest")
        presets_ok = options.max_mb == 3 and options.fps == 15 and options.theme == "light"
        from yui.ui import App
        dnd_app = App.__new__(App)
        dnd_root = dnd_app._make_root()
        dnd_root.withdraw()
        designer_ok = False
        themed_confirmation_ok = False
        source_menu_ok = False
        watermark_controls_ok = toolbox_ok = False
        try:
            dnd_ok = bool(dnd_app.dnd_files and getattr(dnd_root, "TkdndVersion", None))
            if dnd_ok:
                dnd_root.drop_target_register(dnd_app.dnd_files)
                dnd_root.dnd_bind("<<Drop>>", lambda event: "copy")
                dnd_ok = bool(dnd_root.tk.call('bind', str(dnd_root), '<<Drop>>'))
            from yui.designer import Designer
            import time
            editor = Designer(dnd_root, source=unknown_item, options=engine.RenderOptions(),
                              canvas_size=(480, 720), layers=design_layers, on_apply=lambda layers: None)
            editor.geometry("1060x760+12000+12000")
            deadline = time.monotonic() + 20
            try:
                while time.monotonic() < deadline:
                    dnd_root.update()
                    if not editor._loading and editor._rendered_layers == editor.layers:
                        designer_ok = True
                        break
                    time.sleep(.005)
                from yui.dialogs import ConfirmationDialog
                import customtkinter as ctk
                from yui.ui import BG, ACCENT
                prior_mode = ctk.get_appearance_mode()
                checks = []
                for mode, color_index in (("dark", 1), ("light", 0)):
                    ctk.set_appearance_mode(mode)
                    for confirmed in (False, True):
                        prompt = ConfirmationDialog(editor, title="Discard design changes?",
                                                    message="Synthetic confirmation check.")
                        colors_ok = (prompt.cget("fg_color")[color_index] == BG[color_index]
                                     and prompt.confirm_button.cget("fg_color")[color_index] == ACCENT[color_index])
                        dnd_root.after(60, prompt.confirm if confirmed else prompt.cancel)
                        checks.append(colors_ok and prompt.show() is confirmed and editor.grab_current() is editor)
                ctk.set_appearance_mode(prior_mode)
                themed_confirmation_ok = all(checks)
                editor.add_watermark()
                mark_layer = editor.layers[-1]
                watermark_controls_ok = mark_layer.text == "Yui" and mark_layer.opacity == .45
                editor.add_watermark("image", sticker)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and len(editor.layers) < 4:
                    dnd_root.update()
                    time.sleep(.005)
                watermark_controls_ok = (watermark_controls_ok and len(editor.layers) == 4
                                         and editor.layers[-1].kind == "image" and editor.layers[-1].opacity == .45)
            finally:
                editor.close(force=True)
            settings_check = ctk.CTkToplevel(dnd_root)
            settings_check.geometry("720x660+12000+12000")
            try:
                dnd_app.root = dnd_root
                dnd_app._fonts, dnd_app._logo_pil = {}, None
                dnd_app._source_export_busy = False
                tabs = ctk.CTkTabview(settings_check)
                tabs.pack(fill="both", expand=True)
                dnd_app._build_about_tabs(tabs)
                tabs.set("Licenses")
                tabs.cget("command")()
                dnd_root.update()
                controls = dnd_app._license_source_controls
                idle_hidden = controls.winfo_manager() == "" and dnd_app._source_status.cget("text") == ""
                picker = dnd_app._license_picker
                picker.set("FFmpeg source")
                picker.cget("command")("FFmpeg source")
                dnd_root.update()
                selected_visible = controls.winfo_ismapped() and len(dnd_app._source_buttons) == 1
                picker.set("Open-source credits")
                picker.cget("command")("Open-source credits")
                dnd_root.update()
                source_menu_ok = idle_hidden and selected_visible and controls.winfo_manager() == "" and not dnd_app._source_export_busy
            finally:
                settings_check.destroy()
            from yui.toolbox import Toolbox
            tools = Toolbox(dnd_root, items=(timeline_item,), options=edits,
                            on_apply=lambda opts: None, on_split=lambda index, parts: None)
            tools.geometry("1040x780+12000+12000")
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    dnd_root.update()
                    if tools._image is not None and tools._rendered_key == tools._key(edits):
                        toolbox_ok = True
                        break
                    time.sleep(.005)
            finally:
                tools.close(force=True)
        finally:
            dnd_root.destroy()
        from yui import source_bundle
        from zipfile import ZipFile
        source_exports = {}
        for kind in ('ffmpeg',):
            exported = source_bundle.export_source(out_dir / source_bundle.filename(kind), kind)
            with ZipFile(exported) as package:
                source_exports[kind] = package.testzip() is None
        ok = dnd_ok and duration_ok and design_ok and designer_ok and themed_confirmation_ok and source_menu_ok and editing_ok and split_ok and extraction_ok and toolbox_ok and watermark_controls_ok and network_ok and copy_ok and presets_ok and all(source_exports.values()) and all(0 < r.size <= engine.HARD_CAP_BYTES for r in (result, unknown_result, design_result, edited_result))
        report.write_text(json.dumps({
            "ok": ok, "size": result.size, "width": result.width, "height": result.height,
            "fps": result.fps, "seconds": result.seconds, "ffmpeg": "bundled FFmpeg",
            "runtime": {"python": sys.version.split()[0], "ffmpeg": __import__('yui.runtime', fromlist=['FFMPEG_VERSION']).FFMPEG_VERSION},
            "duration_regression": {"ok": duration_ok, "expected_seconds": 5.0,
                                    "detected_seconds": unknown_item.duration, "gif_seconds": actual_seconds,
                                    "size": unknown_result.size},
            "privacy": {"network_protocols_blocked": network_ok},
            "features": {"save_copy_byte_identical": copy_ok, "saved_presets_reload": presets_ok},
            "embedded_source_exports": source_exports,
            "drag_and_drop": {"native_backend_loaded_and_target_registered": dnd_ok},
            "design_editor": {"ok": design_ok and designer_ok, "native_editor_preview": designer_ok,
                              "text_and_sticker_gif": design_frames_ok, "gif_seconds": design_seconds,
                              "metadata_free_png_poster": poster_ok, "gif_size": design_result.size},
            "themed_confirmations": {"ok": themed_confirmation_ok, "dark_and_light": themed_confirmation_ok,
                                     "cancel_and_confirm": themed_confirmation_ok, "nested_grab_restored": themed_confirmation_ok},
            "source_menu": {"ok": source_menu_ok, "persistent_row_removed": source_menu_ok,
                            "component_source_available_on_request": source_menu_ok},
            "editing_tools": {"ok": editing_ok and split_ok and extraction_ok and toolbox_ok and watermark_controls_ok,
                              "crop_effect_reverse_palette": editing_ok, "split": split_ok,
                              "metadata_free_frame_zip": extraction_ok, "native_toolbox_preview": toolbox_ok,
                              "text_and_image_watermark_controls": watermark_controls_ok},
        }, indent=2), encoding="utf-8")
        return 0 if ok else 1
    except Exception as exc:  # pragma: no cover - reported through the json file
        report.write_text(json.dumps({"ok": False, "error": type(exc).__name__}), encoding="utf-8")
        return 1


def main() -> None:
    _ensure_console_streams()
    args = sys.argv[1:]
    if args[:1] == ["--export-app-source"]:
        _close_splash()
        sys.exit(2)  # Application source is private; reject the former export option.
    if args[:1] == ["--export-source"]:
        _close_splash()
        if len(args) != 2:
            sys.exit(2)
        from yui.source_bundle import export_source, SourceExportError
        try:
            export_source(Path(args[1]))
        except SourceExportError:
            sys.exit(1)
        sys.exit(0)
    if args[:1] == ["--selftest"]:
        _close_splash()
        sys.exit(_selftest(Path(args[1]) if len(args) > 1 else Path.cwd() / "yui-selftest"))

    _windows_setup()
    from yui.ui import App, log_exception

    def hook(exc_type, exc, tb):
        log_exception(exc_type, exc, tb)
        if not getattr(sys, "frozen", False):
            sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
    files = [Path(a) for a in args if a and not a.startswith("-")]
    try:
        app = App(files)
    except Exception as exc:
        log_exception(type(exc), exc, exc.__traceback__)
        _close_splash()
        from tkinter import messagebox
        messagebox.showerror("Yui couldn't start", "Try running Yui from a writable local folder or reinstalling it. "
                             "Technical details were saved in Yui's error log.")
        sys.exit(1)
    else:
        _close_splash()
        app.run()


if __name__ == "__main__":
    main()
