"""Synthetic end-to-end checks for poster designs and decorated GIF exports."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image, ImageDraw, PngImagePlugin
from yui import engine
from yui import overlays
from yui.overlays import OverlayLayer
from yui.paths import get_ffmpeg, temp_dir


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    checks = []
    encodes = []

    def check(condition, name):
        assert condition, name
        checks.append(name)

    def rejects(call, name, error=engine.MediaError):
        try:
            call()
        except error:
            checks.append(name)
        else:
            raise AssertionError(name)

    ff = get_ffmpeg()
    source = work / "source café 桜.png"
    image = Image.new("RGB", (320, 240), "#182538")
    pnginfo = PngImagePlugin.PngInfo()
    pnginfo.add_text("Author", "Synthetic private source creator")
    image.info["icc_profile"] = b"Synthetic ICC private identifying bytes"
    image.save(source, pnginfo=pnginfo)
    sticker = work / "sticker transparent 桜.png"
    mark = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(mark).rectangle((10, 10, 53, 53), fill="#ff00ff")
    mark.save(sticker)
    mark2 = work / "sticker animated.gif"
    Image.new("RGB", (30, 30), "red").save(mark2, save_all=True,
        append_images=[Image.new("RGB", (30, 30), "blue")], duration=100, loop=0)
    frame_anim = work / "frames.png"
    Image.new("RGBA", (80, 40), "red").save(frame_anim, save_all=True,
        append_images=[Image.new("RGBA", (80, 40), "blue")], duration=[500, 500], loop=0)
    video = work / "two-colour.mp4"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:r=20:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=20:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video)], check=True)
    original_bytes = {path: path.read_bytes() for path in (source, sticker, mark2, video, frame_anim)}
    item = engine.probe(source)
    movie = engine.probe(video)
    movie.start = .25
    animation = engine.probe(frame_anim)
    opts = engine.RenderOptions(background="#16a34a", fit="fit")
    layers = (
        OverlayLayer(text="Yui\nA poster ✨ 中国", x=.5, y=.22, size=.09, color="#ffffff"),
        OverlayLayer(kind="image", image=sticker, x=.8, y=.75, size=.28),
    )
    checked = overlays.validate_layers(list(layers))
    check(isinstance(checked, tuple) and checked == layers, "validated layers are immutable and preserve order")
    rejects(lambda: setattr(layers[0], "x", .1), "layer models cannot mutate after render starts", Exception)
    tile, bounds = overlays.render_layer(layers[0], (320, 240))
    check(tile.mode == "RGBA" and tile.getbbox() is not None and len(bounds) == 4,
          "Unicode multiline text renders with RGBA bounds")
    image_tile, image_bounds = overlays.render_layer(layers[1], (320, 240))
    check(image_tile.size == (67, 67) and image_bounds == (222, 146, 289, 213),
          "sticker size and centre use normalized canvas coordinates")
    canvas = overlays.compose_overlay(layers, (320, 240))
    check(canvas.size == (320, 240) and canvas.mode == "RGBA" and canvas.getpixel((0, 0))[3] == 0,
          "composition keeps a transparent canvas outside the layers")
    check(canvas.getpixel((256, 180)) == (255, 0, 255, 255), "transparent PNG sticker keeps its opaque centre")
    faded = overlays.compose_overlay((replace(layers[1], opacity=.5),), (320, 240))
    check(faded.getpixel((256, 180))[3] == 128, "layer opacity scales existing image alpha")
    edge = overlays.compose_overlay((replace(layers[1], x=0, y=0, rotation=45),), (320, 240))
    check(edge.getbbox() is not None and edge.getbbox()[0] == 0, "rotated corner layers are safely clipped at the canvas")
    empty = overlays.compose_overlay((OverlayLayer(text=""),), (320, 240))
    check(empty.getbbox() is None, "temporarily empty text produces a transparent layer")
    static, _ = overlays.render_layer(OverlayLayer(kind="image", image=mark2), (320, 240))
    check(static.getpixel((static.width // 2, static.height // 2))[:3] == (255, 0, 0),
          "animated stickers use the first frame as a static design image")

    for name, layer in (
        ("nan position", replace(layers[0], x=math.nan)),
        ("infinite opacity", replace(layers[0], opacity=math.inf)),
        ("huge integer coordinate", replace(layers[0], x=10 ** 10_000)),
        ("off-canvas position", replace(layers[0], x=1.01)),
        ("zero size", replace(layers[0], size=0)),
        ("oversized size", replace(layers[0], size=1.6)),
        ("unsupported colour", replace(layers[0], color="red; movie=anything")),
        ("arbitrary font path", replace(layers[0], font=r"\\server\share\font.ttf")),
        ("oversized text", replace(layers[0], text="a" * 2001)),
        ("control text", replace(layers[0], text="hello\x00world")),
        ("surrogate text", replace(layers[0], text="hello\ud800")),
        ("unknown kind", replace(layers[0], kind="video")),
        ("invalid image type", replace(layers[1], image=123)),
        ("unsupported sticker extension", replace(layers[1], image=work / "bad.svg")),
    ):
        rejects(lambda layer=layer: overlays.validate_layers((layer,)), f"{name} is rejected")
    rejects(lambda: overlays.validate_layers((layers[0],) * 21), "layer count is bounded")
    rejects(lambda: overlays.validate_layers((replace(layers[0], text="a" * 2000),) * 9), "total text budget is bounded")
    rejects(lambda: overlays.compose_overlay(layers, (4096, 4096)), "canvas pixel allocation is bounded", engine.MediaLimitError)
    rejects(lambda: overlays.render_layer(OverlayLayer(text="W" * 2000, size=1.5), (1600, 1600)),
            "oversized wrapped text fails before allocating a tile", engine.MediaLimitError)
    with patch.object(Path, "resolve", side_effect=AssertionError("unsafe resolve")), \
         patch.object(Path, "stat", side_effect=AssertionError("unsafe stat")), \
         patch.object(Path, "open", side_effect=AssertionError("unsafe open")):
        rejects(lambda: overlays.validate_layers((replace(layers[1], image=Path(r"\\server\share\sticker.png")),)),
                "network sticker rejected before filesystem access")
        rejects(lambda: overlays.export_poster(item, opts, layers, Path(r"\\server\share\poster.png"), canvas_size=(320, 240)),
                "network poster destination rejected before filesystem access")

    before_calls = overlays._decoded_sticker.cache_info().misses
    overlays.render_layer(layers[1], (320, 240))
    overlays.render_layer(replace(layers[1], x=.1), (320, 240))
    check(overlays._decoded_sticker.cache_info().misses == before_calls,
          "dragging reuses a bounded decoded sticker rather than reopening the raster")
    mark.putpixel((32, 32), (0, 255, 0, 255)); mark.save(sticker)
    changed, _ = overlays.render_layer(layers[1], (320, 240))
    check(overlays._decoded_sticker.cache_info().misses > before_calls, "changed sticker invalidates the preview decode cache")
    sticker.write_bytes(original_bytes[sticker])
    frozen = overlays.snapshot_layers(layers, work / "snapshots")
    sticker.write_bytes(b"changed after snapshot")
    frozen_canvas = overlays.compose_overlay(frozen, (320, 240))
    check(frozen_canvas.getpixel((256, 180)) == (255, 0, 255, 255), "GIF sticker snapshot survives later original edits")
    sticker.write_bytes(original_bytes[sticker])

    preview = overlays.preview_frame(item, opts, (240, 240))
    check(preview.size == (240, 240) and preview.getpixel((120, 0)) == (22, 163, 74),
          "fit preview uses selected letterbox background")
    filled = overlays.preview_frame(item, replace(opts, fit="fill"), (240, 240))
    check(filled.getpixel((120, 0)) == (24, 37, 56), "fill preview crops to the exact selected canvas")
    anim_blue = overlays.preview_frame(animation, opts, (240, 240), offset=.65)
    check(anim_blue.getpixel((120, 120))[:3] == (0, 0, 255), "animated PNG preview seeks by frame timing")
    video_red = overlays.preview_frame(movie, opts, (320, 240), offset=0)
    video_blue = overlays.preview_frame(movie, opts, (320, 240), offset=1)
    check(video_red.getpixel((160, 120))[0] > 240 and video_blue.getpixel((160, 120))[2] > 240,
          "video preview time includes the source trim start")
    check(overlays.preview_frame(movie, opts, (320, 240), offset=99).size == (320, 240),
          "preview seeks remain within the selected trim end")

    poster = work / "poster.png"; poster.unlink(missing_ok=True)
    exported = overlays.export_poster(item, opts, layers, poster, canvas_size=(320, 240), max_side=640)
    with Image.open(exported) as png:
        check(png.size == (640, 480) and png.format == "PNG", "poster uses the same aspect at full export resolution")
        check(not png.info and png.getexif().get(0x0112) is None, "poster strips ICC profiles, EXIF and private PNG text")
        check(png.getpixel((512, 360)) == (255, 0, 255), "poster includes the sticker above the source frame")
    poster_bytes = poster.read_bytes()
    rejects(lambda: overlays.export_poster(item, opts, layers, source, canvas_size=(320, 240), overwrite=True),
            "poster cannot overwrite its source")
    rejects(lambda: overlays.export_poster(item, opts, layers, sticker, canvas_size=(320, 240), overwrite=True),
            "poster cannot overwrite a sticker source")
    alias = work / "alias-source.png"; alias.unlink(missing_ok=True); os.link(source, alias)
    rejects(lambda: overlays.export_poster(item, opts, layers, alias, canvas_size=(320, 240), overwrite=True),
            "poster cannot overwrite a hard-link alias of the source")
    alias2 = work / "alias-sticker.png"; alias2.unlink(missing_ok=True); os.link(sticker, alias2)
    rejects(lambda: overlays.export_poster(item, opts, layers, alias2, canvas_size=(320, 240), overwrite=True),
            "poster cannot overwrite a hard-link alias of a sticker")
    rejects(lambda: overlays.export_poster(item, opts, layers, video.with_suffix(".gif"), canvas_size=(320, 240)),
            "poster requires a PNG destination")
    occupied = work / "occupied.png"; occupied.write_bytes(b"existing destination")
    unique = overlays.export_poster(item, opts, layers, occupied, canvas_size=(320, 240), max_side=320)
    check(unique != occupied and occupied.read_bytes() == b"existing destination", "poster collision preserves existing files")
    cancelled = threading.Event(); cancelled.set()
    rejects(lambda: overlays.compose_overlay(layers, (320, 240), cancelled.is_set),
            "composition can be cancelled before allocation", engine.RenderCancelled)
    rejects(lambda: overlays.export_poster(item, opts, layers, poster, canvas_size=(320, 240), overwrite=True,
            should_cancel=cancelled.is_set), "poster can be cancelled before reading", engine.RenderCancelled)
    cancelled.clear()
    real_copy = engine.shutil.copyfile
    def cancel_after_copy(*args, **kwargs):
        result = real_copy(*args, **kwargs)
        cancelled.set()
        return result
    before_staging = set(work.glob(".yui-save-*.tmp"))
    with patch("yui.engine.shutil.copyfile", side_effect=cancel_after_copy):
        rejects(lambda: overlays.export_poster(item, opts, layers, poster, canvas_size=(320, 240), max_side=320,
                overwrite=True, should_cancel=cancelled.is_set), "poster cancellation before atomic publish preserves destination",
                engine.RenderCancelled)
    check(poster.read_bytes() == poster_bytes and set(work.glob(".yui-save-*.tmp")) == before_staging,
          "cancelled poster cleans staging files and preserves existing poster")
    cancelled.clear()

    def encode(name, items, options, expected_seconds):
        out = work / f"{name}.gif"; out.unlink(missing_ok=True)
        result = engine.render(items, options, out)
        with Image.open(result.output) as gif:
            duration = 0
            centres = []
            for index in range(gif.n_frames):
                gif.seek(index)
                duration += gif.info.get("duration", 0) / 1000
                raster = gif.convert("RGB")
                centres.append(raster.getpixel((round(raster.width * .8), round(raster.height * .75))))
            # GIF delays use centiseconds and the existing muxer may repeat
            # the final frame; permit at most two of the chosen frame delays.
            check(abs(duration - expected_seconds) <= max(.2, 2 / result.fps),
                  f"{name}: complete timeline survives palette and decoration within frame rounding")
            check(all(colour[0] > 235 and colour[1] < 30 and colour[2] > 235 for colour in centres),
                  f"{name}: stationary sticker remains above every output frame")
            check(result.size == out.stat().st_size <= options.cap_bytes <= engine.HARD_CAP_BYTES,
                  f"{name}: measured output stays within the requested cap")
            encodes.append({"name": name, "size": result.size, "passes": result.passes,
                            "dimensions": gif.size, "frames": gif.n_frames, "seconds": duration})
        return result

    overlay_opts = engine.RenderOptions(overlays=layers, max_side=320, fps=15, dither="none")
    encode("decorated-still", [item], overlay_opts, item.clip_seconds)
    encode("decorated-video", [movie], overlay_opts, movie.clip_seconds)
    encode("decorated-combine", [movie, item], replace(overlay_opts, transition="fade", transition_seconds=.25),
           movie.clip_seconds + item.clip_seconds - .25)
    encode("decorated-boomerang", [movie], replace(overlay_opts, boomerang=True), movie.clip_seconds * 2)
    noisy = work / "noisy.mp4"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
        "testsrc2=s=640x480:r=20:d=6", "-vf", "noise=alls=80:allf=t+u", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(noisy)], check=True)
    encode("decorated-size-cap", [engine.probe(noisy)], replace(overlay_opts, max_mb=.5, max_side=640, fps=20), 6)
    rejects(lambda: engine.render([movie], overlay_opts, sticker, overwrite=True),
            "GIF output cannot replace an image sticker even with overwrite")
    alias3 = work / "alias-sticker.gif"; alias3.unlink(missing_ok=True); os.link(sticker, alias3)
    rejects(lambda: engine.render([movie], overlay_opts, alias3, overwrite=True),
            "GIF output cannot replace a hard-link alias of a sticker")
    cancelled.set()
    refuses = work / "cancelled.gif"; refuses.unlink(missing_ok=True)
    rejects(lambda: engine.render([movie], overlay_opts, refuses, should_cancel=cancelled.is_set),
            "decorated GIF respects cancellation", engine.RenderCancelled)
    check(not refuses.exists() and not list(temp_dir().glob("yui-*")), "cancelled render removes all private temporary files")
    cancelled.clear()

    # Optional previous-release engine provides a true byte comparison rather
    # than duplicating the new implementation in a test expectation.
    if len(sys.argv) > 2:
        old_path = Path(sys.argv[2]).resolve()
        spec = importlib.util.spec_from_file_location("yui._baseline_overlay_engine", old_path)
        baseline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline
        spec.loader.exec_module(baseline)
        baseline_items = [baseline.probe(video), baseline.probe(source)]
        fresh_items = [engine.probe(video), engine.probe(source)]
        before = baseline.render(baseline_items, baseline.RenderOptions(max_side=240, transition="fade", boomerang=True),
                                 work / "baseline.gif", overwrite=True)
        after = engine.render(fresh_items, engine.RenderOptions(max_side=240, transition="fade", boomerang=True),
                              work / "no-overlay.gif", overwrite=True)
        check(before.output.read_bytes() == after.output.read_bytes(), "no-overlay GIF bytes match the prior release exactly")
    check(all(path.read_bytes() == payload for path, payload in original_bytes.items()),
          "text, stickers, poster and GIF renders preserve every original media byte")
    report = {"ok": True, "passed": len(checks), "checks": checks, "encodes": encodes}
    (work / "overlays-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
