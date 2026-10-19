"""End-to-end check: render many mixes of media and assert every GIF stays under its cap.

Run from the project folder:  .venv\\Scripts\\python.exe tests\\smoke_test.py [work_dir]
"""

from __future__ import annotations

import subprocess
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from yui import engine  # noqa: E402
from yui.paths import get_ffmpeg  # noqa: E402

REPORTS = []


def make_media(folder: Path) -> dict[str, Path]:
    ff = get_ffmpeg()
    folder.mkdir(parents=True, exist_ok=True)
    media = {
        "clip": folder / "clip.mp4",
        "noisy": folder / "noisy_long.mp4",
        "portrait": folder / "portrait.png",
        "photo": folder / "photo.jpg",
        "anim_gif": folder / "anim.gif",
        "anim_webp": folder / "anim.webp",
        "unknown_webm": folder / "durationless_5s.webm",
        "unknown_h264": folder / "timestamp_free_5s.mp4",
    }

    def run(args):
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

    if not media["clip"].exists():
        run(["-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=8", "-pix_fmt", "yuv420p", str(media["clip"])])
    if not media["unknown_webm"].exists():
        run([
            "-f", "lavfi", "-i", "testsrc2=s=480x720:r=25:d=5", "-an",
            "-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-live", "1",
            str(media["unknown_webm"]),
        ])
    if not media["unknown_h264"].exists():
        # An elementary H.264 stream has no container duration or packet timestamps.
        run([
            "-f", "lavfi", "-i", "testsrc2=s=480x720:r=25:d=5", "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-f", "h264",
            str(media["unknown_h264"]),
        ])
    if not media["noisy"].exists():
        # Worst case for GIF: full-HD, 90 seconds, heavy per-frame noise.
        run([
            "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=90",
            "-vf", "noise=alls=60:allf=t+u", "-pix_fmt", "yuv420p", "-preset", "ultrafast",
            str(media["noisy"]),
        ])
    if not media["portrait"].exists():
        im = Image.new("RGBA", (1080, 1350), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for i in range(0, 1350, 30):
            d.rectangle([60, i, 1020, i + 14], fill=(255, 107 + i % 120, 157, 255))
        d.ellipse([240, 380, 840, 980], fill=(124, 92, 255, 200))
        im.save(media["portrait"])
    if not media["photo"].exists():
        im = Image.radial_gradient("L").resize((1600, 1000)).convert("RGB")
        im.save(media["photo"], quality=90)
    if not media["anim_gif"].exists():
        frames = []
        for i in range(12):
            f = Image.new("RGB", (320, 240), (20, 20, 30))
            ImageDraw.Draw(f).ellipse([20 + i * 20, 80, 100 + i * 20, 160], fill=(255, 200, 0))
            frames.append(f)
        frames[0].save(media["anim_gif"], save_all=True, append_images=frames[1:], duration=80, loop=0)
    if not media["anim_webp"].exists():
        frames = []
        for i in range(10):
            f = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
            ImageDraw.Draw(f).rectangle([30 * i, 100, 30 * i + 60, 200], fill=(0, 200, 255, 255))
            frames.append(f)
        frames[0].save(media["anim_webp"], save_all=True, append_images=frames[1:], duration=100, loop=0)
    return media


def check(name: str, items, opts: engine.RenderOptions, out: Path, expected_seconds: float = 0) -> None:
    t0 = time.time()
    result = engine.render(items, opts, out)
    took = time.time() - t0
    size = result.output.stat().st_size
    with Image.open(result.output) as im:
        frames = getattr(im, "n_frames", 1)
        dims = im.size
        if expected_seconds:
            seconds = 0.0
            for i in range(frames):
                im.seek(i)
                seconds += im.info.get("duration", 0) / 1000
            assert abs(seconds - expected_seconds) <= 0.15, f"{name}: GIF plays for {seconds}s"
    ok = size <= opts.cap_bytes <= engine.HARD_CAP_BYTES
    print(
        f"{'PASS' if ok else 'FAIL'}  {name:<34} {engine.fmt_size(size):>9} / {opts.cap_bytes / engine.MIB:g} MB"
        f"  {dims[0]}x{dims[1]}  {result.fps:.1f}fps  {frames} frames  {result.seconds:.1f}s"
        f"  passes={result.passes}  {took:.1f}s"
    )
    assert ok, f"{name}: {size} bytes exceeds cap {opts.cap_bytes}"
    assert frames >= 1
    REPORTS.append({"case": name, "size_bytes": size, "cap_bytes": opts.cap_bytes,
                    "width": dims[0], "height": dims[1], "frames": frames, "seconds": result.seconds,
                    "elapsed_seconds": round(took, 3), "passes": result.passes,
                    "playback_seconds": round(seconds, 3) if expected_seconds else None})


def main() -> None:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "yui-smoke"
    work = work.resolve()
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    media = make_media(work / "media")
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    p = lambda key, **kw: engine.probe(media[key], **kw)  # noqa: E731

    check("single clip (defaults)", [p("clip")], engine.RenderOptions(), out / "single.gif")
    check("90s full-HD noise (worst case)", [p("noisy")], engine.RenderOptions(), out / "noisy.gif")

    trimmed = p("clip")
    trimmed.start, trimmed.end = 2.0, 5.0
    check("trimmed 2s-5s, sharp", [trimmed], engine.RenderOptions(priority="sharp"), out / "trim.gif")

    check(
        "boomerang 2x speed, 2 MB cap",
        [p("clip")],
        engine.RenderOptions(max_mb=2, boomerang=True, speed=2.0),
        out / "boomerang.gif",
    )

    combo = [p("clip"), p("portrait", hold=2.0), p("photo", hold=1.5), p("anim_gif"), p("anim_webp")]
    check(
        "combine 5 items + crossfade (fit)",
        combo,
        engine.RenderOptions(transition="fade", background="#ffffff"),
        out / "combo_fade.gif",
    )
    check(
        "combine 5 items, fill 1:1, slide",
        combo,
        engine.RenderOptions(aspect="1:1", fit="fill", transition="slideleft", priority="smooth"),
        out / "combo_fill.gif",
    )
    check(
        "combine noisy video + stills, 5 MB",
        [p("photo", hold=2.0), p("noisy"), p("portrait", hold=2.0)],
        engine.RenderOptions(max_mb=5),
        out / "combo_noisy.gif",
    )
    check("single still image", [p("portrait")], engine.RenderOptions(), out / "still.gif")
    check("animated webp alone", [p("anim_webp")], engine.RenderOptions(loop=-1), out / "webp.gif")
    for key, name in (("unknown_webm", "5s WebM with no duration metadata"),
                      ("unknown_h264", "5s H.264 with no timestamps")):
        item = p(key)
        assert abs(item.duration - 5.0) <= 0.1, f"{name}: detected {item.duration}s"
        assert item.trim_end > 4.9, f"{name}: trim is capped at {item.trim_end}s"
        check(name, [item], engine.RenderOptions(), out / f"{key}.gif", expected_seconds=5.0)
    print("All renders stayed within their size caps.")
    (work / "smoke-test.json").write_text(json.dumps({"ok": True, "cases": REPORTS}, indent=2))


if __name__ == "__main__":
    main()
