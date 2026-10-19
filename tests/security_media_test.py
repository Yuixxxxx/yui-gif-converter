"""Adversarial media/process checks, using only isolated synthetic local files."""

from pathlib import Path
import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui import engine
from yui.preview import GifPlayer, _Unavailable
from yui.paths import get_ffmpeg


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    ffmpeg = sys.argv[2] if len(sys.argv) > 2 else get_ffmpeg()
    child_python = getattr(sys, "_base_executable", sys.executable)
    checks = []

    def check(condition, name):
        assert condition, name
        checks.append(name)

    def rejects(call, error, name):
        try:
            call()
        except error:
            checks.append(name)
        else:
            raise AssertionError(name)

    # This is a harmless local canary; the demuxer must reject the manifest
    # before opening it, even when the manifest has a supported .mp4 suffix.
    canary = work / "nested_probe_canary.ts"
    canary.write_bytes(b"Synthetic test canary")
    manifest = work / "disguised.mp4"
    manifest.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nnested_probe_canary.ts\n#EXT-X-ENDLIST\n")
    result = engine._run_capture([ffmpeg, "-hide_banner", "-loglevel", "debug", "-i", str(manifest)])
    check(result.returncode != 0, "renamed HLS playlist rejected")
    check(canary.name not in result.stderr, "nested local reference not opened")
    rejects(lambda: engine.probe(manifest, ffmpeg), engine.MediaError, "renamed playlist rejected by public import")
    result = engine._run_capture([ffmpeg, "-hide_banner", "-loglevel", "debug", "-f", "hls", "-i", str(manifest)])
    check(result.returncode != 0 and "not on whitelist" in result.stderr.lower(), "HLS demuxer cannot bypass format allowlist")
    check(canary.name not in result.stderr, "forced HLS reference not opened")
    malicious_concat = work / "disguised.mov"
    malicious_concat.write_text("ffconcat version 1.0\nfile 'nested_probe_canary.ts'\n")
    result = engine._run_capture([ffmpeg, "-hide_banner", "-loglevel", "debug", "-i", str(malicious_concat)])
    check(result.returncode != 0 and "not on whitelist" in result.stderr.lower(), "renamed concat playlist rejected")
    check(canary.name not in result.stderr, "nested concat reference not opened")

    gif = work / "small.gif"
    Image.new("RGB", (16, 16), "red").save(gif, format="GIF")
    payload = gif.read_bytes()
    for side in (9000, 20000):
        bomb = work / f"canvas-{side}.gif"
        bomb.write_bytes(payload[:6] + side.to_bytes(2, "little") * 2 + payload[10:])
        with patch.object(engine, "_probe_ffmpeg", side_effect=AssertionError("Unsafe fallback")), \
                patch.object(Image.Image, "load", side_effect=AssertionError("Canvas allocation")):
            rejects(lambda: engine.probe(bomb, ffmpeg), engine.MediaError,
                    f"{side}px canvas rejected before allocation or decoder fallback")
        ready = queue.Queue(maxsize=3)
        stats = {"cache_bytes": 0, "queue_max": 0, "decoded_frames": 0,
                 "fully_cached": False, "raster_max": 0}
        GifPlayer._decode(bomb, (160, 100), ready, threading.Event(), stats)
        check(isinstance(ready.get_nowait(), _Unavailable) and stats["decoded_frames"] == 0,
              f"{side}px preview rejected before decoding")
    too_large = work / "too-large.gif"
    with too_large.open("wb") as file:
        file.write(payload)
        file.truncate(engine.HARD_CAP_BYTES + 1)
    ready = queue.Queue(maxsize=3)
    stats = {"cache_bytes": 0, "queue_max": 0, "decoded_frames": 0,
             "fully_cached": False, "raster_max": 0}
    with patch.object(Image, "open", side_effect=AssertionError("Oversized preview decoded")):
        GifPlayer._decode(too_large, (160, 100), ready, threading.Event(), stats)
    check(isinstance(ready.get_nowait(), _Unavailable), "preview byte limit applied before Pillow")

    # Use real child processes to prove silent hangs do not need stdout to
    # become cancellable. This command is Python, so no FFmpeg flags apply.
    actual_popen = engine.subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        child = actual_popen(*args, **kwargs)
        children.append(child)
        return child

    holder = {}
    before = time.monotonic()
    with patch.object(engine.subprocess, "Popen", side_effect=spawn):
        rejects(lambda: engine._run_capture([sys.executable, "-c", "import time; time.sleep(30)"],
                                            timeout=.12, proc_holder=holder), subprocess.TimeoutExpired,
                "silent child times out")
    check(time.monotonic() - before < 2 and holder["proc"] is None and children[-1].poll() is not None,
          "timed-out process reaped and holder released")
    cancelled = threading.Event()
    timer = threading.Timer(.12, cancelled.set)
    timer.start()
    with patch.object(engine.subprocess, "Popen", side_effect=spawn):
        rejects(lambda: engine._run_capture([sys.executable, "-c", "import time; time.sleep(30)"],
                                            should_cancel=cancelled.is_set, proc_holder=holder),
                engine.RenderCancelled, "silent child cancellation works")
    timer.join()
    check(holder["proc"] is None and children[-1].poll() is not None, "cancelled process reaped")
    noise = engine._run_capture([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*12000000); sys.stderr.buffer.write(b'y'*500000)"], binary=True)
    check(len(noise.stdout) == engine.MAX_CAPTURE_BYTES and len(noise.stderr) == engine.MAX_ERROR_BYTES,
          "both captured streams are bounded and drained")

    runner = engine._Runner(ffmpeg, work, lambda *args: None, lambda: False, holder)
    # Redirect only the child launch, preserving _Runner's monitoring path.
    def silent_runner(*args, **kwargs):
        return spawn([child_python, "-c", "import time; time.sleep(30)"], **kwargs)
    before = time.monotonic()
    with patch.object(engine.subprocess, "Popen", side_effect=silent_runner), \
            patch.object(engine._Runner, "MIN_TIMEOUT", .12), patch.object(engine._Runner, "MAX_TIMEOUT", .12):
        rejects(lambda: runner.run([], 1, 0, 1, "Testing"), engine.MediaError, "renderer silent hang has friendly deadline")
    check(time.monotonic() - before < 2 and children[-1].poll() is not None and holder["proc"] is None,
          "renderer deadline kills and reaps child")
    def private_error(*args, **kwargs):
        return spawn([child_python, "-c", "import sys; sys.stderr.write('C:/Users/PrivateUser/secret.mp4'); sys.exit(1)"], **kwargs)
    with patch.object(engine.subprocess, "Popen", side_effect=private_error):
        try:
            runner.run([], 1, 0, 1, "Testing")
        except engine.MediaError as exc:
            check("PrivateUser" not in str(exc) and "C:/" not in str(exc), "renderer error omits private decoder paths")
        else:
            raise AssertionError("Failing child succeeded")

    clip = work / "five-seconds.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=160x100:r=12:d=5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                   check=True, capture_output=True, **engine._no_window())
    item = engine.probe(clip, ffmpeg)
    check(abs(item.duration - 5) < .15, "normal five-second video probes correctly")
    check(engine.make_thumbnail(item, ffmpeg=ffmpeg).width > 0, "normal thumbnail works")
    with patch.object(engine, "get_ffmpeg", return_value=ffmpeg):
        result = engine.render([item], engine.RenderOptions(), work / "normal.gif")
    with Image.open(result.output) as image:
        seconds = 0
        for frame in range(image.n_frames):
            image.seek(frame)
            seconds += image.info.get("duration", 0) / 1000
    check(abs(seconds - 5) < .15 and result.size <= engine.HARD_CAP_BYTES,
          "normal conversion preserves five-second timing and hard size cap")
    animation = work / "normal.webp"
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue", "green")]
    frames[0].save(animation, format="WEBP", save_all=True, append_images=frames[1:], duration=150, loop=0)
    item = engine.probe(animation, ffmpeg)
    rejects(lambda: engine._explode_animation(item, work / "limited-animation", max_bytes=1),
            engine.MediaLimitError, "animation preparation stops at temporary disk budget")
    enormous_plan = engine.Plan(480, 480, 10, (10000,), boomerang=True)
    rejects(lambda: engine._build_graph([], enormous_plan, engine.RenderOptions(), "{in}null[out]"),
            engine.MediaLimitError, "boomerang memory budget rejects huge buffering before FFmpeg")
    with patch.object(engine, "get_ffmpeg", return_value=ffmpeg):
        result = engine.render([item], engine.RenderOptions(), work / "normal-webp.gif")
    check(result.output.is_file() and 0 < result.size <= engine.HARD_CAP_BYTES,
          "generated relative concat preserves animated WebP conversion")
    report = {"ok": True, "checks": checks, "passed": len(checks)}
    (work / "security-media-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
