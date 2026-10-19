"""Exercise preview timing, worker isolation, cancellation and bounded buffers."""

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


class Widget:
    """A timed Tk stand-in that rejects access from the decoder thread."""

    def __init__(self, display_cost=0):
        self.owner = threading.get_ident()
        self.jobs = []
        self.cancelled = set()
        self.sequence = 0
        self.records = []
        self.values = {}
        self.display_cost = display_cost

    def check_thread(self):
        assert threading.get_ident() == self.owner, "A worker accessed a Tk widget"

    def winfo_width(self):
        self.check_thread()
        return 664

    def winfo_height(self):
        self.check_thread()
        return 424

    def configure(self, **kwargs):
        self.check_thread()
        self.values.update(kwargs)
        image = kwargs.get("image")
        if image is not None:
            self.records.append((time.monotonic(), image.image.getpixel((0, 0)), image.size))
            if self.display_cost:
                time.sleep(self.display_cost)

    def after(self, milliseconds, fn):
        self.check_thread()
        self.sequence += 1
        heapq.heappush(self.jobs, (time.monotonic() + milliseconds / 1000, self.sequence, fn))
        return self.sequence

    def after_cancel(self, job):
        self.check_thread()
        self.cancelled.add(job)

    def run_until(self, condition, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            if self.jobs and self.jobs[0][0] <= time.monotonic():
                _, job, fn = heapq.heappop(self.jobs)
                if job not in self.cancelled:
                    fn()
            else:
                time.sleep(0.001)
        raise AssertionError("Preview did not reach the expected state")

    def pending(self):
        return [job for job in self.jobs if job[1] not in self.cancelled]


class TkImage:
    owner = threading.get_ident()

    def __init__(self, light_image, dark_image, size):
        assert threading.get_ident() == self.owner, "A worker created a Tk image"
        self.image = light_image
        self.size = size


def gif(path, frames=8, size=(320, 200), duration=50, color=None):
    images = [Image.new("RGB", size, color or (index * 20, 20, 40)) for index in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], duration=duration, loop=0)


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    clip = work / "timed-preview.gif"
    gif(clip)
    reports = {}
    convert_threads = []
    original_convert = Image.Image.convert

    def convert(image, *args, **kwargs):
        convert_threads.append(threading.get_ident())
        return original_convert(image, *args, **kwargs)

    with patch("yui.preview.ctk.CTkImage", TkImage), \
            patch("yui.preview.ctk.ScalingTracker.get_widget_scaling", return_value=1.0), \
            patch.object(Image.Image, "convert", convert):
        label = Widget(display_cost=0.012)
        player = GifPlayer(label, label)
        before = time.monotonic()
        player.load(clip)
        load_ms = (time.monotonic() - before) * 1000
        label.run_until(lambda: len([r for r in label.records if r[1][:3] == (0, 20, 40)]) >= 2)
        beginnings = [r[0] for r in label.records if r[1][:3] == (0, 20, 40)]
        cycle_ms = (beginnings[1] - beginnings[0]) * 1000
        assert abs(cycle_ms - 400) < 45, f"Display work changed playback timing: {cycle_ms:.1f}ms"
        assert load_ms < 30, f"load blocked the UI for {load_ms:.1f}ms"
        assert convert_threads and all(t != label.owner for t in convert_threads)
        assert player._stats["queue_max"] <= player.READY_FRAMES
        assert player._stats["cache_bytes"] <= player.CACHE_BYTES
        assert player._stats["fully_cached"]
        assert player._stats["decoded_frames"] == 8, "A cached loop was decoded again"
        # The decoder's BytesIO never keeps the original GIF open.
        renamed = clip.with_name("renamed-preview.gif")
        clip.rename(renamed)
        renamed.rename(clip)
        worker = player._worker
        player.stop()
        worker.join(0.3)
        assert not worker.is_alive() and not label.pending()
        reports["timing"] = {"source_cycle_ms": 400, "measured_cycle_ms": round(cycle_ms, 2),
                             "simulated_ui_frame_cost_ms": 12, "load_return_ms": round(load_ms, 2),
                             "cached_loop_decoded_once": True, "original_file_unlocked": True}

        variable = work / "variable-delay-preview.gif"
        gif(variable, frames=4, duration=[10, 30, 70, 110])
        label = Widget(display_cost=0.012)
        player = GifPlayer(label, label)
        player.load(variable)
        # Track the long frame: a 10 ms frame can correctly be skipped when the
        # simulated display cost alone takes longer than its encoded delay.
        label.run_until(lambda: len([r for r in label.records if r[1][:3] == (60, 20, 40)]) >= 2)
        beginnings = [r[0] for r in label.records if r[1][:3] == (60, 20, 40)]
        variable_ms = (beginnings[1] - beginnings[0]) * 1000
        assert abs(variable_ms - 220) < 35, f"Variable GIF delays were changed: {variable_ms:.1f}ms"
        player.stop()
        reports["variable_delays"] = {"encoded_delays_ms": [10, 30, 70, 110],
                                      "source_cycle_ms": 220, "measured_cycle_ms": round(variable_ms, 2)}

        still = work / "single-frame.gif"
        gif(still, frames=1)
        label = Widget()
        player = GifPlayer(label, label)
        player.load(still)
        label.run_until(lambda: len(label.records) == 1)
        player._worker.join(0.3)
        assert not player._worker.is_alive() and not label.pending()
        player.stop()
        reports["single_frame_no_animation_timer"] = True

        slow = work / "slow-old-preview.gif"
        replacement = work / "new-preview.gif"
        gif(slow, frames=1, color=(240, 0, 0))
        gif(replacement, frames=1, color=(0, 240, 0))
        original_read = Path.read_bytes

        def slow_read(path):
            if path == slow:
                time.sleep(0.08)
            return original_read(path)

        label = Widget()
        player = GifPlayer(label, label)
        with patch.object(Path, "read_bytes", slow_read):
            player.load(slow)
            old_worker = player._worker
            old_cancel = player._cancel
            player.load(replacement)
            label.run_until(lambda: bool(label.records))
            old_worker.join(0.3)
        assert old_cancel.is_set() and not old_worker.is_alive()
        assert all(record[1][:3] == (0, 240, 0) for record in label.records)
        player.stop()
        reports["rapid_load_discards_stale_frames"] = True

        label = Widget()
        player = GifPlayer(label, label)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Remote file access")):
            player.load(Path(r"\\private-server\share\preview.gif"))
        assert "local drive" in label.values["text"] and not label.pending()
        reports["network_share_blocked_before_read"] = True

        many = work / "streamed-preview.gif"
        gif(many, frames=12, size=(640, 400), duration=10)
        label = Widget()
        player = GifPlayer(label, label)
        with patch.object(GifPlayer, "CACHE_BYTES", 500_000):
            player.load(many)
            time.sleep(0.08)
            assert player._ready.qsize() <= player.READY_FRAMES
            label.run_until(lambda: player._stats["decoded_frames"] > 12)
            stats = dict(player._stats)
            assert stats["cache_bytes"] == 0 and not stats["fully_cached"]
            assert stats["raster_max"] <= player.MAX_RASTER_SIDE
            worker = player._worker
            player.stop()
            worker.join(0.3)
            assert not worker.is_alive()
        reports["bounded_streaming"] = {"ready_queue_limit": GifPlayer.READY_FRAMES,
                                        "observed_queue_max": stats["queue_max"],
                                        "cache_overflow_released": True,
                                        "raster_side_limit": GifPlayer.MAX_RASTER_SIDE,
                                        "observed_raster_max": stats["raster_max"]}

    # Also exercise the actual Tk/CTkImage integration without showing a window.
    import customtkinter as ctk

    root = ctk.CTk()
    root.withdraw()
    label = ctk.CTkLabel(root, text="")
    box = ctk.CTkFrame(root, width=640, height=400)
    player = GifPlayer(label, box)
    try:
        player.load(clip)
        end = time.monotonic() + 2
        while label.cget("image") is None and time.monotonic() < end:
            root.update()
            time.sleep(0.002)
        assert isinstance(label.cget("image"), ctk.CTkImage), "Actual Tk preview did not display a frame"
        player.stop()
        assert label.cget("image") is None and player.job is None
        reports["hidden_tk_integration"] = True
    finally:
        player.stop()
        root.destroy()

    report = {"ok": True, "worker_owns_pillow_decode": True, "tk_access_on_main_thread": True, **reports}
    (work / "preview-test.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
