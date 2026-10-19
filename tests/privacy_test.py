"""Check local-only media processing, metadata removal and private error logs."""

from pathlib import Path
import json
import os
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yui import AUTHOR, engine, legal  # noqa: E402
from yui.paths import fallback_output_dir, get_ffmpeg, log_path  # noqa: E402
from yui.ui import App, log_exception  # noqa: E402


def main():
    work = Path(sys.argv[1])
    work.mkdir(parents=True, exist_ok=True)
    os.environ["APPDATA"] = str(work / "profile")
    os.environ["LOCALAPPDATA"] = str(work / "local-profile")
    ff = get_ffmpeg()
    remote = work / "remote-reference.m3u8"
    remote.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nhttp://127.0.0.1:9/blocked.ts\n#EXT-X-ENDLIST\n")
    blocked = engine._run_capture([ff, "-hide_banner", "-nostdin", "-loglevel", "error",
                                   "-i", str(remote), "-f", "null", "-"])
    assert blocked.returncode != 0 and "not on whitelist" in blocked.stderr.lower(), blocked.stderr
    with patch("pathlib.Path.is_file", side_effect=AssertionError("A network path was accessed")):
        try:
            engine.probe(Path(r"\\private-server\share\video.mp4"))
        except engine.MediaError:
            pass
        else:
            raise AssertionError("Network share was accepted")
    if os.name == "nt":
        with patch("ctypes.windll.kernel32.GetDriveTypeW", return_value=4):
            try:
                engine.probe(Path("Z:/video.mp4"))
            except engine.MediaError:
                pass
            else:
                raise AssertionError("Mapped network drive was accepted")
    app = App.__new__(App)
    app._set_status = lambda *args, **kwargs: None
    with patch.dict(os.environ, {"APPDATA": r"\\private-server\profile"}):
        with patch("pathlib.Path.unlink", side_effect=AssertionError("A network path was accessed")), \
             patch("yui.ui.subprocess.run", side_effect=AssertionError("A network shortcut was created")):
            app._set_sendto(False)
            app._set_sendto(True)
    source = work / "tagged.mp4"
    sentinel = "YUI_TEST_PRIVATE_METADATA"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=s=256x144:r=12:d=2", "-metadata", f"artist={sentinel}",
                    "-metadata", f"comment={sentinel}", str(source)], check=True,
                   capture_output=True, creationflags=engine.CREATE_NO_WINDOW)
    result = engine.render([engine.probe(source)], engine.RenderOptions(), work / "private.gif")
    assert sentinel.encode() not in result.output.read_bytes()
    assert 0 < result.size <= engine.HARD_CAP_BYTES
    default_output = engine.output_path(source)
    assert default_output.parent == fallback_output_dir()
    try:
        raise ValueError(r"C:\Users\PrivateUser\secret\private_video.mp4")
    except ValueError as exc:
        log_exception(type(exc), exc, exc.__traceback__)
    log = log_path().read_text(encoding="utf-8")
    assert "PrivateUser" not in log and "private_video.mp4" not in log and "C:\\Users" not in log
    assert AUTHOR == "Yui"
    assert "© 2026 Yui" in legal.document("notices") or "Yui" in legal.document("terms")
    report = {"ok": True, "network_protocols_blocked": True, "network_shares_blocked": True,
              "source_metadata_removed": True, "private_logs": True, "local_default_output": True,
              "publisher": AUTHOR}
    (work / "privacy-test.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
