"""Real-media crop, effect, reversal and palette regression checks."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import replace
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image, ImageDraw
from yui import engine, overlays, transforms
from yui.paths import get_ffmpeg


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    checks = []
    exports = []

    def check(condition, label):
        assert condition, label
        checks.append(label)

    def reject(call, label, exception=engine.MediaError):
        try:
            call()
        except exception:
            checks.append(label)
        else:
            raise AssertionError(label)

    still = work / "split-colour.png"
    image = Image.new("RGB", (320, 240), (240, 30, 20))
    ImageDraw.Draw(image).rectangle((160, 0, 319, 239), fill=(20, 40, 230))
    image.save(still)
    animation = work / "two-frames.png"
    image.save(animation, save_all=True, append_images=[Image.new("RGB", (320, 240), "green")],
               duration=[1000, 1000], loop=0)
    video = work / "red-blue.mp4"
    ff = get_ffmpeg()
    engine._run_capture([ff, "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(still),
                        "-t", "1", "-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(video)])
    moving = work / "moving.mp4"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=1",
        "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=10:d=1",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[out]", "-map", "[out]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(moving)], check=True)
    sources = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (still, animation, video, moving)}
    opts = engine.RenderOptions(max_side=320, fps=10, max_mb=.5)
    crop = replace(opts, crop=(.5, 0, .5, 1))
    check(transforms.crop_pixels((320, 240), crop.crop) == (160, 0, 320, 240), "normalised crop maps to exact pixel rectangle")
    check(transforms.crop_pixels((7, 5), (.99, .99, .01, .01)) == (6, 4, 7, 5), "tiny edge crop retains one pixel inside source")
    full = replace(opts, crop=[0, 0, 1, 1])
    check(transforms.validate_edit_options(full) is full and full.crop is None, "full crop normalises to original path")

    for value in ((-1, 0, 1, 1), (0, 0, 0, 1), (.8, 0, .3, 1), (float("nan"), 0, 1, 1),
                  (0, 0, float("inf"), 1), (False, 0, 1, 1), (10**1000, 0, 1, 1), "crop", (0, 0, 1)):
        reject(lambda value=value: engine.estimate([engine.probe(still)], replace(opts, crop=value)), f"invalid crop rejected: {str(value)[:40]}")
    for values in ({"effect": "hue=7;movie=remote"}, {"effect": []}, {"reverse": 1}, {"palette_colors": True},
                   {"palette_colors": 3}, {"palette_colors": 257}, {"palette_mode": "remote"}, {"palette_mode": "diff"}, {"palette_mode": []}):
        reject(lambda values=values: transforms.validate_edit_options(replace(opts, **values)), f"unsupported edit options rejected: {values}")

    def encoded(items, options, name):
        result = engine.render(items, options, work / f"{name}.gif")
        frames, seconds, colors = [], 0, set()
        with Image.open(result.output) as gif:
            while True:
                frame = gif.convert("RGB")
                frames.append(frame.copy())
                seconds += gif.info.get("duration", 0) / 1000
                colors.update(frame.get_flattened_data())
                try:
                    gif.seek(gif.tell() + 1)
                except EOFError:
                    break
        check(result.size <= options.cap_bytes, f"{name} obeys measured GIF size cap")
        exports.append({"name": name, "size": result.size, "seconds": seconds, "colors": len(colors), "width": result.width, "height": result.height})
        return result, frames, seconds, colors

    for path in (still, video, animation):
        item = engine.probe(path)
        item.hold = .5
        plan = engine.estimate([item], crop)
        check((plan.width, plan.height) == (160, 240), f"{path.suffix} automatic aspect follows cropped dimensions")
        preview = overlays.preview_frame(item, crop, (160, 240))
        check(preview.getpixel((80, 120))[2] > 210 and preview.getpixel((80, 120))[0] < 35,
              f"{path.suffix} preview crops to blue half")
        result, frames, seconds, _ = encoded([item], crop, f"cropped-{path.stem}")
        first = frames[0].getpixel((80, 120))
        check(first[2] > 205 and first[0] < 40 and (result.width, result.height) == (160, 240),
              f"{path.suffix} encoded crop matches preview and preserves source detail")

    coloured = work / "sample-colour.png"
    Image.new("RGBA", (96, 96), (170, 90, 45, 255)).save(coloured)
    for effect in transforms.EFFECTS:
        option = replace(opts, effect=effect)
        expected = transforms.image_transform(Image.new("RGB", (96, 96), (170, 90, 45)), option).getpixel((48, 48))
        result, frames, _, _ = encoded([engine.probe(coloured, hold=.3)], option, f"effect-{effect}")
        actual = frames[0].getpixel((result.width // 2, result.height // 2))
        check(max(abs(a-b) for a, b in zip(actual, expected)) <= 3, f"{effect} GIF matches Pillow preview colour matrix")
    transparent = transforms.image_transform(Image.new("RGBA", (20, 20), (170, 90, 45, 73)), replace(opts, effect="sepia"))
    check(transparent.getpixel((10, 10))[3] == 73, "effects preserve original image alpha")
    tiny = work / "tiny.png"
    Image.new("RGB", (24, 24), (170, 90, 45)).save(tiny)
    tiny_item = engine.probe(tiny, hold=.3)
    tiny_opts = replace(opts, crop=(.25, 0, .5, 1))
    tiny_plan = engine.estimate([tiny_item], tiny_opts)
    tiny_preview = overlays.preview_frame(tiny_item, tiny_opts, (tiny_plan.width, tiny_plan.height))
    result, frames, _, _ = encoded([tiny_item], tiny_opts, "tiny-crop")
    check(tiny_preview.getpixel((0, 0)) == (170, 90, 45) and
          max(abs(a-b) for a,b in zip(tiny_preview.getpixel((0, 0)), frames[0].getpixel((0, 0)))) <= 3,
          "small still and tight crop preview upscale to match actual GIF canvas")
    oriented = work / "camera-rotation.jpg"
    camera = Image.new("RGB", (80, 40), (240, 30, 20))
    ImageDraw.Draw(camera).rectangle((40, 0, 79, 39), fill=(20, 40, 230))
    exif = Image.Exif()
    exif[274] = 6
    camera.save(oriented, exif=exif, quality=100, subsampling=0)
    oriented_item = engine.probe(oriented, hold=.3)
    oriented_opts = replace(opts, crop=(0, .5, 1, .5))
    oriented_plan = engine.estimate([oriented_item], oriented_opts)
    preview = overlays.preview_frame(oriented_item, oriented_opts, (oriented_plan.width, oriented_plan.height))
    result, frames, _, _ = encoded([oriented_item], oriented_opts, "exif-crop")
    check((oriented_item.width, oriented_item.height) == (40, 80) and result.width == result.height
          and preview.getpixel((24, 24))[2] > 210 and frames[0].getpixel((24, 24))[2] > 210,
          "crop coordinates follow EXIF-correct camera orientation in GIF and preview")

    movie = engine.probe(moving)
    backward = replace(opts, reverse=True)
    result, frames, seconds, _ = encoded([movie], backward, "reverse-video")
    check(frames[0].getpixel((80, 60))[2] > 220 and frames[-1].getpixel((80, 60))[0] > 220,
          "reverse plays the complete video backward")
    check(abs(seconds - 2) <= .2 and abs(result.seconds - 2) < .001, "reverse retains source duration")
    preview = overlays.preview_frame(movie, backward, (160, 120), offset=0)
    check(preview.getpixel((80, 60))[2] > 220, "reverse initial preview shows source final frame")
    preview = overlays.preview_frame(movie, backward, (160, 120), offset=1.9)
    check(preview.getpixel((80, 60))[0] > 220, "reverse final preview shows source initial frame")
    result, frames, seconds, _ = encoded([movie], replace(backward, boomerang=True), "reverse-boomerang")
    check(frames[0].getpixel((80, 60))[2] > 220 and frames[-1].getpixel((80, 60))[2] > 220
          and abs(seconds - 4) <= .3, "reverse and boomerang work independently and compose in order")
    red, blue = work / "red.png", work / "blue.png"
    Image.new("RGB", (160, 120), "red").save(red)
    Image.new("RGB", (160, 120), "blue").save(blue)
    _, frames, _, _ = encoded([engine.probe(red, hold=.4), engine.probe(blue, hold=.4)], backward, "reverse-joined")
    check(frames[0].getpixel((80, 60))[2] > 220 and frames[-1].getpixel((80, 60))[0] > 220,
          "reverse reverses the joined image sequence")
    oversized = engine.Plan(1600, 1600, 2, (60.,))
    reject(lambda: engine._build_graph([], oversized, backward, "{in}null[out]"),
           "reverse working set is bounded before FFmpeg starts", engine.MediaLimitError)

    gradient = work / "gradient.png"
    raster = Image.new("RGB", (160, 120))
    raster.putdata([((x * 3) % 256, (y * 5) % 256, (x * y) % 256) for y in range(120) for x in range(160)])
    raster.save(gradient)
    for colors, mode in ((4, "auto"), (16, "full"), (32, "auto")):
        _, _, _, used = encoded([engine.probe(gradient, hold=.3)], replace(opts, palette_colors=colors, palette_mode=mode), f"palette-{colors}-{mode}")
        check(len(used) <= colors, f"{colors}-colour {mode} palette limits actual visible RGB colours")

    sticker = work / "mark.png"
    Image.new("RGBA", (32, 32), (255, 0, 255, 255)).save(sticker)
    layer = overlays.OverlayLayer(kind="image", image=sticker, x=.5, y=.5, size=.3)
    combined = replace(backward, crop=(.5, 0, .5, 1), effect="grayscale", overlays=(layer,))
    result, frames, _, _ = encoded([movie], combined, "combined-edits")
    check(all(frame.getpixel((result.width // 2, result.height // 2)) == (255, 0, 255) for frame in frames),
          "watermark remains above crop effect and every reversed frame")
    poster = overlays.export_poster(movie, combined, (layer,), work / "poster.png", canvas_size=(60, 120), max_side=240)
    with Image.open(poster) as png:
        check(png.getpixel((60, 120))[:3] == (255, 0, 255) and not png.info,
              "poster shares transformed source and metadata-free watermark composition")

    if len(sys.argv) > 2:
        baseline_zip = Path(sys.argv[2])
        with zipfile.ZipFile(baseline_zip) as zf:
            entry = next(n for n in zf.namelist() if n.endswith("yui/engine.py"))
            baseline_path = work / "baseline_engine.py"
            baseline_path.write_bytes(zf.read(entry))
        spec = importlib.util.spec_from_file_location("yui._edit_baseline", baseline_path)
        baseline = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = baseline
        spec.loader.exec_module(baseline)
        baseline_result = baseline.render([baseline.probe(still, hold=.3)], baseline.RenderOptions(max_side=160, fps=10, max_mb=.5), work / "baseline-still.gif")
        fresh = engine.render([engine.probe(still, hold=.3)], engine.RenderOptions(max_side=160, fps=10, max_mb=.5), work / "new-still.gif")
        check(baseline_result.output.read_bytes() == fresh.output.read_bytes(), "unedited still GIF is byte-identical to shipped 1.5.2 engine")
        baseline_result = baseline.render([baseline.probe(moving)], baseline.RenderOptions(max_side=160, fps=10, max_mb=.5), work / "baseline-default.gif")
        fresh, frames, _, _ = encoded([engine.probe(moving)], engine.RenderOptions(max_side=160, fps=10, max_mb=.5), "new-default")
        with Image.open(baseline_result.output) as old:
            old.seek(old.n_frames - 1)
            old_final = old.convert("RGB").getpixel((80, 60))
        check(old_final[0] > 220 and frames[0].getpixel((80, 60))[0] > 220 and frames[-1].getpixel((80, 60))[2] > 220,
              "automatic palette fixes old scene-change colour loss while retaining both scenes")
    check(all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in sources.items()),
          "all original media remain byte-identical")
    report = {"ok": True, "passed": len(checks), "checks": checks, "exports": exports}
    (work / "edit-engine-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "passed": len(checks), "report": str(work / "edit-engine-report.json")}))


if __name__ == "__main__":
    main()
