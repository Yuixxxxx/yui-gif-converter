"""Exercise unchanged GIF copies, local-path guards and safe publication failures."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from PIL import Image
from PIL.GifImagePlugin import GifImageFile
from yui import engine


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = os.environ["TMP"] = str(work)
    checks = []

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

    source = work / "original.gif"
    frames = [Image.new("RGB", (24, 18), colour) for colour in ("red", "blue", "green")]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[50, 140, 270],
                   loop=3, comment=b"Synthetic unchanged-copy fixture")
    original = source.read_bytes()
    target = work / "副本 café" / "桜 Yui.gif"
    target.unlink(missing_ok=True)
    copied = engine.save_copy(source, target)
    check(copied == target and copied.read_bytes() == original, "unicode destination preserves every byte")
    with Image.open(copied) as image:
        delays = []
        for index in range(image.n_frames):
            image.seek(index)
            delays.append(image.info["duration"])
        check(delays == [50, 140, 270] and image.info["loop"] == 3,
              "frame delays and loop metadata remain unchanged")
    check(source.read_bytes() == original, "source remains untouched")

    occupied = work / "occupied.gif"
    occupied.write_bytes(b"Existing destination stays intact")
    collision = engine.save_copy(source, occupied)
    check(collision != occupied and collision.read_bytes() == original and
          occupied.read_bytes() == b"Existing destination stays intact", "occupied name creates a separate copy")

    replaced = engine.save_copy(source, occupied, overwrite=True)
    check(replaced == occupied and occupied.read_bytes() == original, "explicit overwrite publishes complete GIF")
    occupied.write_bytes(b"Original destination")
    before = set(work.glob(".yui-save-*.tmp"))
    with patch("yui.engine.shutil.copyfile", side_effect=OSError("synthetic copy failure")):
        rejects(lambda: engine.save_copy(source, occupied, overwrite=True), "copy failure is reported")
    check(occupied.read_bytes() == b"Original destination", "copy failure preserves existing destination")
    with patch("yui.engine.os.replace", side_effect=OSError("synthetic replacement failure")):
        rejects(lambda: engine.save_copy(source, occupied, overwrite=True), "replacement failure is reported")
    check(occupied.read_bytes() == b"Original destination", "replacement failure preserves existing destination")
    check(set(work.glob(".yui-save-*.tmp")) == before and not list(work.glob("yui-copy-*")),
          "failed copies clean up both staging files")

    rejects(lambda: engine.save_copy(source, source, overwrite=True), "source cannot be overwritten")
    alias = work / "hard-link.gif"
    alias.unlink(missing_ok=True)
    os.link(source, alias)
    rejects(lambda: engine.save_copy(source, alias, overwrite=True), "hard-link alias cannot overwrite source")
    check(alias.read_bytes() == original and source.read_bytes() == original, "source guards preserve original bytes")
    rejects(lambda: engine.save_copy(source, work / "invalid.png"), "non-GIF destination extension is rejected")

    # Any filesystem calls here would access a network path before the guard.
    with patch.object(Path, "resolve", side_effect=AssertionError("unexpected filesystem access")), \
         patch.object(Path, "exists", side_effect=AssertionError("unexpected filesystem access")), \
         patch.object(Path, "stat", side_effect=AssertionError("unexpected filesystem access")), \
         patch.object(Path, "open", side_effect=AssertionError("unexpected filesystem access")):
        rejects(lambda: engine.save_copy(Path(r"\\server\share\clip.gif"), target),
                "network source rejected before filesystem access")
        rejects(lambda: engine.save_copy(source, Path(r"\\server\share\copy.gif")),
                "network destination rejected before filesystem access")
    with patch("yui.engine.is_local_path", return_value=False), \
         patch.object(Path, "stat", side_effect=AssertionError("unexpected filesystem access")):
        rejects(lambda: engine.save_copy(source, target), "mapped network drive rejected before file access")

    empty = work / "empty.gif"; empty.write_bytes(b"")
    malformed = work / "malformed.gif"; malformed.write_bytes(original[:25])
    wrong = work / "wrong.gif"; Image.new("RGB", (12, 12)).save(wrong, format="PNG")
    oversized = work / "oversized.gif"
    with oversized.open("wb") as fh:
        fh.truncate(engine.HARD_CAP_BYTES + 1)
    for path, name in ((empty, "empty GIF rejected"), (malformed, "damaged GIF rejected"),
                       (wrong, "non-GIF bytes rejected"), (oversized, "GIF over hard cap rejected"),
                       (work / "missing.gif", "missing source rejected"), (work, "directory source rejected")):
        rejects(lambda p=path: engine.save_copy(p, work / "uncreated.gif"), name)
    check(not (work / "uncreated.gif").exists(), "invalid sources create no destination")
    enormous = work / "huge-canvas.gif"
    enormous.write_bytes(original[:6] + (10_000).to_bytes(2, "little") * 2 + original[10:])
    with patch.object(GifImageFile, "load", side_effect=AssertionError("Huge raster allocation attempted")):
        rejects(lambda: engine.save_copy(enormous, work / "uncreated.gif"),
                "tiny file with enormous logical canvas rejected before raster allocation")

    cancelled = threading.Event()
    cancelled.set()
    rejects(lambda: engine.save_copy(source, work / "cancelled.gif", should_cancel=cancelled.is_set),
            "cancellation before reading is reported", engine.RenderCancelled)
    cancelled.clear()
    real_copy = engine.shutil.copyfile
    def cancel_after_copy(*args, **kwargs):
        result = real_copy(*args, **kwargs)
        cancelled.set()
        return result
    with patch("yui.engine.shutil.copyfile", side_effect=cancel_after_copy):
        rejects(lambda: engine.save_copy(source, occupied, overwrite=True, should_cancel=cancelled.is_set),
                "cancellation before publication is reported", engine.RenderCancelled)
    check(occupied.read_bytes() == b"Original destination" and not (work / "cancelled.gif").exists(),
          "cancelled copy preserves destination and creates no partial output")
    check(set(work.glob(".yui-save-*.tmp")) == before and not list(work.glob("yui-copy-*")),
          "cancelled copies remove staging files")

    race = work / "race.gif"
    race.unlink(missing_ok=True)
    real_unique = engine.unique_path
    injected = False
    def collide(path):
        nonlocal injected
        candidate = real_unique(path)
        if not injected:
            candidate.write_bytes(b"Concurrent save")
            injected = True
        return candidate
    with patch("yui.engine.unique_path", side_effect=collide):
        raced_copy = engine.save_copy(source, race)
    check(race.read_bytes() == b"Concurrent save" and raced_copy != race and raced_copy.read_bytes() == original,
          "collision during publication preserves concurrent file")

    real_publish = engine._publish_output
    snapshot_target = work / "snapshot.gif"
    snapshot_target.unlink(missing_ok=True)
    def change_source(snapshot, output, overwrite, cancel):
        source.write_bytes(b"Original changed after validation")
        return real_publish(snapshot, output, overwrite, cancel)
    try:
        with patch("yui.engine._publish_output", side_effect=change_source):
            snapshot_copy = engine.save_copy(source, snapshot_target)
        check(snapshot_copy.read_bytes() == original, "source change cannot replace the validated snapshot")
    finally:
        source.write_bytes(original)
    report = {"passed": len(checks), "checks": checks}
    (work / "save-copy-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
