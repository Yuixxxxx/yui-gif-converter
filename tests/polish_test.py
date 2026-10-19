"""Exercise large-list interactions, coalesced saves and lazy legal content."""

from pathlib import Path
import json
import os
import sys
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from yui import TERMS_VERSION, engine, legal
from yui.settings import Settings, SettingsWriter
from yui.ui import App


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    profile = work / uuid.uuid4().hex
    os.environ['APPDATA'] = str(profile / 'profile')
    os.environ['LOCALAPPDATA'] = str(profile / 'local')
    checks = []
    def check(condition, name):
        assert condition, name
        checks.append(name)

    saves = []
    main_thread = threading.get_ident()
    def capture(settings):
        saves.append((settings.speed, threading.get_ident()))
        time.sleep(.04)
    with patch.object(Settings, 'save', capture):
        writer = SettingsWriter(delay=.04)
        settings = Settings()
        before = time.monotonic()
        for i in range(20):
            settings.speed = 1 + i / 100
            writer.submit(settings)
        elapsed = time.monotonic() - before
        settings.speed = 7
        time.sleep(.1)
        check(saves == [(1.19, saves[0][1])], 'rapid edits coalesce into an independent latest snapshot')
        check(saves[0][1] != main_thread and elapsed < .1, 'slow preference writes never run on the UI thread')
        settings.speed = 1.5
        writer.close(settings)
        check(saves[-1][0] == 1.5 and not writer._thread.is_alive(), 'closing flushes the latest edit and stops the writer')

    # A newer edit arriving while a disk write is already in progress wins.
    started, release = threading.Event(), threading.Event()
    ordered = []
    actual_save = Settings.save
    def blocked_save(settings):
        if not ordered:
            started.set()
            release.wait(2)
        ordered.append(settings.speed)
        actual_save(settings)
    with patch.object(Settings, 'save', blocked_save):
        writer = SettingsWriter(delay=.001)
        settings = Settings(speed=1.25)
        writer.submit(settings)
        check(started.wait(1), 'background save starts')
        settings.speed = 2
        writer.submit(settings)
        release.set()
        writer.close(settings)
    check(Settings.load().speed == 2 and ordered == [1.25, 2], 'in-flight saves cannot overwrite newer preferences')
    with patch('yui.settings.os.replace', side_effect=OSError('synthetic disk error')):
        writer = SettingsWriter()
        writer.close(Settings(speed=4))
    check(Settings.load().speed == 2 and not list(Settings.path().parent.glob('.settings-*.tmp')),
          'save failure preserves prior settings and cleans temporary files')

    def root(app):
        app.dnd_files = None
        result = ctk.CTk()
        result.withdraw()
        return result

    errors = []
    with patch.object(App, '_make_root', root), patch.object(Settings, 'load', return_value=Settings(
            accepted_terms=TERMS_VERSION, sound=False)):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        def spin_until(test, seconds=8):
            until = time.monotonic() + seconds
            while not test() and time.monotonic() < until:
                app.root.update()
                time.sleep(.002)
            check(test(), 'event loop completed pending work')
        def items(count):
            return [engine.MediaItem(work / f'clip-{i}.mp4', 'video', 320, 240, duration=5, fps=25)
                    for i in range(count)]
        try:
            app.root.update()
            app.items = items(80)
            beats = []
            alive = True
            def heartbeat():
                if not alive:
                    return
                beats.append(time.monotonic())
                app.root.after(10, heartbeat)
            heartbeat()
            before = time.monotonic()
            app._rebuild_rows()
            initial_ms = (time.monotonic() - before) * 1000
            check(len(app.rows) < 80 and app._building_rows, 'large lists yield before creating all rows')
            check(app.primary.cget('state') == 'disabled', 'conversion waits for editable rows')
            with patch.object(app, '_start') as start:
                app.on_primary()
                check(not start.called, 'keyboard conversion also waits for rows')
            first = app.rows[0]
            first.start_entry.delete(0, 'end'); first.start_entry.insert(0, '1.25')
            app.move_item(first.item, 1)
            spin_until(lambda: not app._building_rows)
            alive = False
            check(len(beats) > 4, 'UI heartbeat runs during large-list creation')
            check(len(app.rows) == 80 and [row.item for row in app.rows] == app.items,
                  'all rows finish in current order after mid-build reorder')
            check(app.rows[1] is first and first.start_entry.get() == '1.25',
                  'mid-build reorder preserves unfinished trim edits')
            check(app.primary.cget('state') == 'normal', 'conversion is restored when list finishes')
            with patch.object(app, '_start'), patch.object(app, '_refresh_summary', wraps=app._refresh_summary) as refresh:
                app.on_primary()
                check(refresh.call_count == 1 and first.item.trim_start == 1.25,
                      'bulk commit updates summary once and preserves trim')
            app.clear_items()
            app.items = items(60)
            app._rebuild_rows()
            app.root.after(1, app.clear_items)
            spin_until(lambda: not app.items)
            app.root.update()
            check(not app.rows and not app._building_rows and app._rows_job is None,
                  'Clear cancels unfinished row creation without resurrecting rows')

            with patch.object(legal, 'document', wraps=legal.document) as documents:
                app.open_settings()
                top = app._settings_window
                tabs = next(w for w in top.winfo_children() if isinstance(w, ctk.CTkTabview))
                check(not documents.called, 'opening General does not load legal documents')
                for name, key in [('Terms', 'terms'), ('Privacy', 'privacy'), ('Licenses', 'notices')]:
                    tabs.set(name); tabs.cget('command')()
                    check(any(call.args == (key,) for call in documents.call_args_list), f'{name} opens its document on demand')
                count = documents.call_count
                tabs.set('Terms'); tabs.cget('command')()
                check(documents.call_count == count, 'revisiting a document reuses its contents')
                lic = tabs.tab('Licenses')
                box = next(w for w in lic.winfo_children() if isinstance(w, ctk.CTkTextbox))
                picker = next(w for w in lic.winfo_children() if isinstance(w, ctk.CTkOptionMenu))
                choose = picker.cget('command')
                for label, path in legal.license_files():
                    choose(label)
                    check(box.get('1.0', 'end-1c') == path.read_text(encoding='utf-8'), f'full {label} license is accessible')
                choose('FFmpeg (GPLv3)'); box.yview_moveto(1)
                choose('Open-source credits')
                check(box.yview()[0] == 0, 'changing license resets its scroll position')
                check(picker.cget('dynamic_resizing') is False, 'license titles cannot stretch settings')
                top.destroy()
            check(not errors, 'no Tk callback errors')
        finally:
            app._on_close()
    gap_ms = max((b-a)*1000 for a,b in zip(beats, beats[1:]))
    report = {'ok': True, 'checks': checks, 'count': len(checks),
              'initial_80_row_callback_ms': round(initial_ms, 2),
              'largest_heartbeat_gap_ms': round(gap_ms, 2),
              'inspection': 'isolated hidden Tk runtime; timing is machine-specific'}
    (work / 'polish-test.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
