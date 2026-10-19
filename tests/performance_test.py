r"""Check identical GIF output with bounded filtering and cancellation during preparation.

Run: .venv\Scripts\python.exe tests\performance_test.py MEDIA_FOLDER [WORK_FOLDER]
MEDIA_FOLDER is the existing smoke-test media folder. Timing is reported, not asserted.
"""
from __future__ import annotations

import hashlib
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yui import engine  # noqa: E402


def main() -> None:
    media = Path(sys.argv[1])
    work = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.gettempdir()) / "yui-performance"
    work.mkdir(parents=True, exist_ok=True)
    clip = engine.probe(media / "clip.mp4")
    noisy = engine.probe(media / "noisy_long.mp4")
    noisy.end = 4
    cases = [
        ("video", [clip], engine.RenderOptions()),
        ("crossfade", [clip, engine.probe(media / "portrait.png"), engine.probe(media / "photo.jpg")],
         engine.RenderOptions(transition="fade", background="#ffffff")),
        ("noisy_clip", [noisy], engine.RenderOptions(max_mb=2)),
    ]
    original_run = engine._Runner.run
    measurements = []
    hashes = {}
    try:
        for mode in ("automatic", "bounded", "bounded", "automatic", "automatic", "bounded"):
            def run(self, args, *a, **kw):
                if mode == "automatic":
                    args = ["-filter_complex_threads", "0", *args]
                return original_run(self, args, *a, **kw)
            engine._Runner.run = run
            for name, items, opts in cases:
                output = work / f"{name}-{mode}.gif"
                started = time.perf_counter()
                result = engine.render(items, opts, output, overwrite=True)
                elapsed = time.perf_counter() - started
                assert 0 < result.size <= opts.cap_bytes <= engine.HARD_CAP_BYTES
                digest = hashlib.sha256(output.read_bytes()).hexdigest()
                assert hashes.setdefault(name, digest) == digest, f"{name}: filtering changed the GIF"
                measurements.append({"case": name, "mode": mode, "seconds": elapsed})
    finally:
        engine._Runner.run = original_run

    calls = 0
    def cancel_during_frames() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 4

    cancel_work = work / "cancel-preparation"
    def new_workdir(**kwargs) -> str:
        cancel_work.mkdir()
        return str(cancel_work)

    animated = engine.probe(media / "anim.webp")
    cancelled_output = work / "cancelled.gif"
    with patch.object(engine.tempfile, "mkdtemp", side_effect=new_workdir), \
         patch.object(engine._Runner, "run", side_effect=AssertionError("Cancelled preparation started FFmpeg")):
        try:
            engine.render([animated], engine.RenderOptions(), cancelled_output, should_cancel=cancel_during_frames)
            raise AssertionError("Preparation did not stop after cancellation")
        except engine.RenderCancelled:
            pass
    assert not cancel_work.exists(), "Cancelled temporary frames were not cleaned up"
    assert not cancelled_output.exists()

    timings = []
    for name, _, _ in cases:
        medians = {mode: statistics.median(row["seconds"] for row in measurements
                                           if row["case"] == name and row["mode"] == mode)
                   for mode in ("automatic", "bounded")}
        timings.append({"case": name, "automatic_seconds": round(medians["automatic"], 3),
                        "bounded_seconds": round(medians["bounded"], 3),
                        "change_percent": round((1 - medians["bounded"] / medians["automatic"]) * 100, 1),
                        "byte_identical_output": True})
    report = {"ok": True, "filter_workers": engine.FILTER_THREADS, "timings": timings,
              "cancelled_animation_preparation": True, "temporary_frames_cleaned": True,
              "note": "Local median of three renders per case; speed depends on media and hardware."}
    (work / "performance-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
