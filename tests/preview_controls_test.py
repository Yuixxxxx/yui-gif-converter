"""Check pause/resume timing and restart/cancellation without external UI access."""

from pathlib import Path
import heapq
import json
import sys
import threading
import time
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui.preview import GifPlayer  # noqa: E402
from preview_test import TkImage, Widget, gif  # noqa: E402


def run_for(label, seconds):
    """Dispatch the stand-in widget's timers for a bounded interval."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if label.jobs and label.jobs[0][0] <= time.monotonic():
            _, job, fn = heapq.heappop(label.jobs)
            if job not in label.cancelled:
                fn()
        else:
            time.sleep(0.001)


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    clip = work / "preview-controls.gif"
    gif(clip, frames=4, duration=[300, 120, 70, 180])
    reports = {}
    workers = []

    with patch("yui.preview.ctk.CTkImage", TkImage), \
            patch("yui.preview.ctk.ScalingTracker.get_widget_scaling", return_value=1.0):
        label = Widget()
        player = GifPlayer(label, label)
        player.load(clip)
        workers.append(player._worker)
        label.run_until(lambda: bool(label.records))
        assert player.playable and not player.paused
        run_for(label, 0.09)
        assert player.pause() and player.paused
        remaining = player._remaining
        frozen_frame = player._current
        record_count = len(label.records)
        assert 0.14 < remaining < 0.27, f"Unexpected remaining delay: {remaining:.3f}s"
        assert player.job is None and not label.pending()
        assert not player.pause(), "Repeated pause changed state"
        run_for(label, 0.42)
        assert len(label.records) == record_count and player._current is frozen_frame
        assert player._ready.qsize() <= player.READY_FRAMES
        assert player._stats["queue_max"] <= player.READY_FRAMES
        assert player._stats["cache_bytes"] <= player.CACHE_BYTES
        # Pausing must not lock the file even while its producer is waiting.
        renamed = work / "paused-renamed.gif"
        clip.rename(renamed)
        renamed.rename(clip)
        resumed = time.monotonic()
        assert player.resume() and not player.paused
        assert not player.resume(), "Repeated resume scheduled another timer"
        assert len(label.pending()) == 1
        label.run_until(lambda: len(label.records) > record_count)
        resumed_delay = label.records[record_count][0] - resumed
        assert abs(resumed_delay - remaining) < 0.045, "Resume changed the frame's remaining delay"
        assert label.records[record_count][1][:3] == (20, 20, 40), "Resume skipped frames after a pause"
        label.run_until(lambda: len(label.records) > record_count + 1)
        second_delay = label.records[record_count + 1][0] - label.records[record_count][0]
        assert abs(second_delay - 0.12) < 0.035, "Variable frame delay changed after resume"
        reports["pause_resume"] = {
            "frame_unchanged_while_paused": True,
            "no_animation_timer_while_paused": True,
            "pause_ms": 420,
            "remaining_delay_ms": round(remaining * 1000, 2),
            "resumed_delay_ms": round(resumed_delay * 1000, 2),
            "next_encoded_delay_ms": 120,
            "next_measured_delay_ms": round(second_delay * 1000, 2),
            "no_catch_up_after_pause": True,
            "original_file_unlocked": True,
        }

        for _ in range(12):
            assert player.pause()
            assert not label.pending()
            assert player.resume()
            assert len(label.pending()) == 1
        reports["repeated_toggles_leave_one_timer"] = True

        assert player.pause()
        old_generation = player._generation
        old_cancel = player._cancel
        old_worker = player._worker
        previous_records = len(label.records)
        assert player.restart()
        workers.append(player._worker)
        assert player._generation > old_generation and old_cancel.is_set() and not player.paused
        label.run_until(lambda: len(label.records) > previous_records)
        assert label.records[previous_records][1][:3] == (0, 20, 40), "Restart did not display frame 0"
        old_worker.join(0.3)
        assert not old_worker.is_alive(), "Restart left the old producer waiting"
        player.stop()
        assert not player.paused and not player.playable and player._path is None
        assert player.job is None and not label.pending()
        assert not player.restart()
        reports["restart_resets_generation_and_frame"] = True

        # An early pause request cannot strand a preview in its loading state.
        reading = threading.Event()
        allow_read = threading.Event()
        original_read = Path.open

        def delayed_read(path, *args, **kwargs):
            reading.set()
            assert allow_read.wait(1), "Test did not release the decoder"
            return original_read(path, *args, **kwargs)

        label = Widget()
        player = GifPlayer(label, label)
        with patch.object(Path, "open", delayed_read):
            player.load(clip)
            workers.append(player._worker)
            assert reading.wait(0.5)
            assert not player.playable and not player.pause() and not player.paused
            allow_read.set()
            label.run_until(lambda: player.playable)
        assert player.pause()
        player.load(clip)
        workers.append(player._worker)
        assert not player.paused
        label.run_until(lambda: player.playable)
        player.stop()
        reports["loading_pause_is_safe_and_load_resets_pause"] = True

        # Rapid replacement while paused cancels each bounded producer and timer.
        label = Widget()
        player = GifPlayer(label, label)
        for _ in range(10):
            player.load(clip)
            workers.append(player._worker)
            label.run_until(lambda: player.playable)
            assert player.pause()
        player.stop()
        assert not label.pending() and not player.paused
        for worker in workers:
            worker.join(0.3)
            assert not worker.is_alive(), "A cancelled preview worker survived"
        reports["rapid_paused_replacement_releases_all_workers"] = True

        still = work / "preview-controls-still.gif"
        gif(still, frames=1)
        label = Widget()
        player = GifPlayer(label, label)
        player.load(still)
        label.run_until(lambda: bool(label.records))
        player._worker.join(0.3)
        assert not player.playable and not player.paused
        assert not player.pause() and not player.resume() and not player.restart()
        assert player.job is None and not label.pending()
        player.stop()
        reports["static_preview_has_no_controls_or_timers"] = True

    # Check the public controls against real hidden CTk widgets as well.
    import customtkinter as ctk

    root = ctk.CTk()
    root.withdraw()
    label = ctk.CTkLabel(root, text="")
    box = ctk.CTkFrame(root, width=640, height=400)
    player = GifPlayer(label, box)
    try:
        player.load(clip)
        deadline = time.monotonic() + 2
        while not player.playable and time.monotonic() < deadline:
            root.update()
            time.sleep(0.002)
        assert player.playable and isinstance(label.cget("image"), ctk.CTkImage)
        assert player.pause()
        image = label.cget("image")
        end = time.monotonic() + 0.36
        while time.monotonic() < end:
            root.update()
            time.sleep(0.002)
        assert label.cget("image") is image and player.job is None
        assert player.resume()
        end = time.monotonic() + 1
        while label.cget("image") is image and time.monotonic() < end:
            root.update()
            time.sleep(0.002)
        assert label.cget("image") is not image
        assert player.restart() and not player.paused
        end = time.monotonic() + 1
        while not player.playable and time.monotonic() < end:
            root.update()
            time.sleep(0.002)
        assert player.playable and player.pause()
        player.load(still)
        end = time.monotonic() + 1
        while label.cget("image") is None and time.monotonic() < end:
            root.update()
            time.sleep(0.002)
        assert label.cget("image") is not None and not player.paused and not player.playable
        assert player.job is None
        player.load(clip)
        end = time.monotonic() + 1
        while not player.playable and time.monotonic() < end:
            root.update()
            time.sleep(0.002)
        assert player.playable
        reports["hidden_tk_controls_integration"] = True
    finally:
        player.stop()
        worker = player._worker
        if worker is not None:
            worker.join(0.3)
        root.destroy()

    report = {"ok": True, "checks": len(reports), **reports}
    (work / "preview-controls-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
