"""Exercise embedded source export, atomic failures and live Settings controls."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui import TERMS_VERSION, source_bundle


def main():
    work = Path(sys.argv[1]).resolve() / uuid.uuid4().hex
    work.mkdir(parents=True)
    os.environ['APPDATA'] = str(work / 'profile')
    os.environ['LOCALAPPDATA'] = str(work / 'local')
    checks = []
    def check(value, name):
        assert value, name
        checks.append(name)

    def rejected(action, name, error=source_bundle.SourceExportError):
        try:
            action()
        except error as exc:
            check(str(work) not in str(exc), name + ' has no personal path')
        else:
            raise AssertionError(name)
        check(not list(work.glob('.yui-source-*.tmp')), name + ' leaves no partial files')

    from app import _ensure_console_streams
    with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None):
        _ensure_console_streams()
        try:
            check(sys.stderr.write('font fallback warning\n') == 22, 'windowed startup warnings have a valid stderr')
            check(sys.stdout.write('status\n') == 7, 'windowed startup has a valid stdout')
            sys.stderr.flush()
            sys.stdout.flush()
        finally:
            sys.stdout.close()
            sys.stderr.close()
    with patch.object(sys, 'stdout') as output, patch.object(sys, 'stderr') as error:
        _ensure_console_streams()
        check(sys.stdout is output and sys.stderr is error, 'normal console streams are preserved')

    manifest = json.loads((ROOT / 'vendor/source-manifest.json').read_text())
    check(set(manifest['sources']) == {'ffmpeg'}, 'manifest exposes only the required FFmpeg component source')
    private = work / 'private-app.zip'
    with patch.object(source_bundle, '_source_stream') as reader:
        rejected(lambda: source_bundle.export_source(private, 'app'), 'application source cannot be exported')
        check(not private.exists() and not reader.called, 'app source rejection creates no file and reads no source')
    for kind in ('ffmpeg',):
        path = source_bundle.export_source(work / source_bundle.filename(kind), kind)
        check(hashlib.sha256(path.read_bytes()).hexdigest() == manifest['sources'][kind]['sha256'],
              kind + ' source export matches the release manifest')
        with ZipFile(path) as archive:
            check(archive.testzip() is None, kind + ' export is a complete source ZIP')
        before = path.read_bytes()
        rejected(lambda: source_bundle.export_source(path, kind), kind + ' existing file is protected')
        check(path.read_bytes() == before, kind + ' overwrite refusal preserves prior contents')

    # Read an executable-shaped ZIP overlay without executing anything.
    frozen = work / 'embedded.exe'
    frozen.write_bytes(b'MZ' + bytes(512) + (ROOT / 'vendor/source-payload.zip').read_bytes())
    with patch.object(source_bundle, 'is_frozen', return_value=True), patch.object(sys, 'executable', str(frozen)):
        for kind in ('ffmpeg',):
            path = source_bundle.export_source(work / ('frozen-' + kind + '.zip'), kind)
            check(hashlib.sha256(path.read_bytes()).hexdigest() == manifest['sources'][kind]['sha256'],
                  kind + ' can be recovered from a standard executable ZIP overlay')
    rejected(lambda: source_bundle.export_source(work / 'wrong.txt'), 'non-ZIP filenames are rejected')
    rejected(lambda: source_bundle.export_source(work / 'unknown.zip', 'unknown'), 'unknown source kind is rejected')
    rejected(lambda: source_bundle.export_source(work / 'cancel.zip', cancelled=lambda: True),
             'cancellation before export creates no output', source_bundle.SourceExportCancelled)
    with patch.object(source_bundle, 'is_local_path', return_value=False), patch.object(source_bundle, '_source_stream') as reader:
        rejected(lambda: source_bundle.export_source(work / 'unsafe.zip'), 'nonlocal destination is rejected')
        check(not reader.called, 'unsafe destination is rejected before reading source')
    original = work / 'prior.zip'
    original.write_bytes(b'original file')
    payload = b'x' * (2 * 1024 * 1024)
    entry = {'size_bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    @contextmanager
    def stream(kind, metadata=entry):
        yield io.BytesIO(payload), metadata
    with patch.object(source_bundle, '_source_stream', side_effect=lambda kind: stream(kind, {**entry, 'sha256': '0' * 64})):
        rejected(lambda: source_bundle.export_source(original, overwrite=True), 'hash failure is atomic')
    check(original.read_bytes() == b'original file', 'hash failure preserves existing output')
    calls = []
    def cancel_midstream():
        calls.append(True)
        return len(calls) >= 3
    with patch.object(source_bundle, '_source_stream', stream):
        rejected(lambda: source_bundle.export_source(original, overwrite=True, cancelled=cancel_midstream),
                 'midstream cancellation is atomic', source_bundle.SourceExportCancelled)
        with patch.object(source_bundle.os, 'replace', side_effect=OSError('synthetic failure')):
            rejected(lambda: source_bundle.export_source(original, overwrite=True), 'publish failure is atomic')
        with patch.object(source_bundle, 'MAX_SOURCE_BYTES', 1024):
            rejected(lambda: source_bundle.export_source(original, overwrite=True), 'oversized source is bounded')
    check(original.read_bytes() == b'original file', 'all failed writes preserve the existing file')

    import customtkinter as ctk
    from yui.settings import Settings
    from yui.ui import App
    main_thread = threading.get_ident()
    def hidden_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.withdraw()
        return root
    errors = []
    with patch.object(App, '_make_root', hidden_root), patch.object(Settings, 'load', return_value=Settings(
            accepted_terms=TERMS_VERSION, sound=False)):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        def pump_until(condition):
            until = time.monotonic() + 8
            while not condition() and time.monotonic() < until:
                app.root.update()
                time.sleep(.002)
            assert condition(), 'Background source export did not finish'
        try:
            app.open_settings()
            app.root.update()
            picker = app._license_picker
            picker.master.master.set('Licenses')
            app.root.update()
            check(picker.get() == 'Open-source credits', 'Licenses opens on the existing credits view')
            check('FFmpeg source' in picker.cget('values'), 'FFmpeg source remains available as a Licenses choice')
            check(app._license_source_controls.winfo_manager() == '',
                  'default credits view removes source controls from the layout')
            check(not app._license_source_controls.winfo_ismapped(),
                  'default credits view does not show a persistent source row')
            check(app._source_status.cget('text') == '', 'idle source status has no explanatory sentence')
            def select_license(choice):
                app._license_picker.set(choice)
                app._license_picker._command(choice)
                app.root.update()
            with patch('tkinter.filedialog.asksaveasfilename') as dialog, patch.object(source_bundle, 'export_source') as exporter:
                select_license('FFmpeg source')
                check(app._license_source_controls.winfo_manager() == 'grid',
                      'selecting FFmpeg source restores its export controls to the layout')
                check(not dialog.called and not exporter.called and not app._source_export_busy,
                      'selecting FFmpeg source does not automatically start an export')
                select_license('Open-source credits')
                check(app._license_source_controls.winfo_manager() == '' and
                      not app._license_source_controls.winfo_ismapped(),
                      'switching back to credits hides the source controls')
                select_license('FFmpeg source')
            check(len(app._source_buttons) == 1 and app._source_buttons[0].cget('text') == 'Save FFmpeg source…',
                  'Settings offers only the FFmpeg component source export')
            with patch('tkinter.filedialog.asksaveasfilename', return_value=''), patch.object(source_bundle, 'export_source') as exporter:
                app._source_buttons[0].invoke()
                check(not app._source_export_busy and not exporter.called, 'cancelled save dialog has no side effects')
            entered, release = threading.Event(), threading.Event()
            threads = []
            actual_export = source_bundle.export_source
            def delayed_export(*args, **kwargs):
                threads.append(threading.get_ident())
                entered.set()
                assert release.wait(5)
                return actual_export(*args, **kwargs)
            saved = work / 'ui-ffmpeg.zip'
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(saved)) as dialog, patch.object(source_bundle, 'export_source', delayed_export):
                app._source_buttons[0].invoke()
                check(entered.wait(2) and app._source_export_busy, 'source export starts in the background')
                check(all(button.cget('state') == 'disabled' for button in app._source_buttons), 'buttons disable during source export')
                app._save_bundled_source('ffmpeg')
                check(dialog.call_count == 1, 'duplicate clicks cannot start another export')
                beat = []
                app.root.after(1, lambda: beat.append(True))
                pump_until(lambda: bool(beat))
                check(threads == [threads[0]] and threads[0] != main_thread, 'UI continues processing while source export is blocked')
                app._settings_window.destroy()
                app.open_settings()
                app._license_picker.master.master.set('Licenses')
                select_license('FFmpeg source')
                check(app._source_status.cget('text') == 'Saving source ZIP…', 'reopened Settings keeps active export status')
                release.set()
                pump_until(lambda: not app._source_export_busy)
            check(hashlib.sha256(saved.read_bytes()).hexdigest() == manifest['sources']['ffmpeg']['sha256'], 'Settings exports exact FFmpeg source bytes')
            check(all(button.cget('state') == 'normal' for button in app._source_buttons), 'buttons restore after success')
            check(app._source_status.cget('text') == 'Source ZIP saved.', 'successful export displays confirmation')
            with patch('tkinter.filedialog.asksaveasfilename', return_value=str(work / 'failed.zip')), patch.object(source_bundle, 'export_source', side_effect=source_bundle.SourceExportError('Source integrity check failed. Download Yui again.')):
                app._source_buttons[0].invoke()
                pump_until(lambda: not app._source_export_busy)
            check('integrity check failed' in app._source_status.cget('text'), 'failed export displays a friendly error')
            check(all(button.cget('state') == 'normal' for button in app._source_buttons), 'failed export can be retried')
            check(not errors, 'closing and reopening Settings produces no Tk callback errors')
        finally:
            app._on_close()
    report = {'ok': True, 'count': len(checks), 'checks': checks, 'inspection': 'real exports plus isolated hidden Tk Settings runtime'}
    (work.parent / 'in-app-bundle-test.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
