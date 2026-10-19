"""Exercise media tools through the real App using isolated synthetic media."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ['APPDATA'] = str(work / 'profile')
    os.environ['LOCALAPPDATA'] = str(work / 'local')
    import customtkinter as ctk
    from PIL import Image
    from yui import TERMS_VERSION, engine
    from yui.settings import Settings
    from yui.ui import App
    from yui.overlays import OverlayLayer

    source = work / 'synthetic.gif'
    frames = [Image.new('RGB', (160, 120), color) for color in ('red', 'green', 'blue', 'yellow')]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=500, loop=0)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    checks, errors = [], []

    def check(value, name):
        assert value, name
        checks.append(name)

    def hidden_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.geometry('1120x740+12000+12000')
        return root

    with patch.object(App, '_make_root', hidden_root), patch.object(Settings, 'load', return_value=Settings(
            accepted_terms=TERMS_VERSION, sound=False, max_side=160, fps=8, output_dir=str(work / 'GIFs'))):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)

        def until(condition, seconds=20):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not condition():
                app.root.update()
                time.sleep(.005)
            check(condition(), 'background work completes')

        try:
            check(app.toolbox_button.cget('state') == 'disabled', 'toolbox disabled until media is imported')
            app.items = [engine.probe(source)]
            app._rebuild_rows()
            app._show_view('list')
            until(lambda: not app._building_rows)
            check(app.toolbox_button.cget('state') == 'normal', 'import enables media tools')
            app.design_layers = (OverlayLayer(text='Yui', x=.8, y=.9, size=.08, opacity=.45),)
            app.open_toolbox()
            box = app._toolbox
            check(box is not None and box.options.overlays == app.design_layers, 'toolbox receives static watermarks')
            until(lambda: box._image is not None and box._rendered_key is not None)
            app.open_toolbox()
            check(app._toolbox is box, 'reopening reuses the same draft window')
            edited = replace(app.settings.render_options(), crop=(.25, 0, .5, 1), effect='grayscale',
                             reverse=True, palette_colors=32, palette_mode='full')
            app._apply_tool_options(edited)
            check(app.settings.crop == edited.crop and app.settings.effect == 'grayscale' and app.settings.reverse,
                  'toolbox options reach application settings')
            check('cropped' in app.summary.cget('text') and 'reversed' in app.summary.cget('text'),
                  'summary describes active crop and reverse')
            check(app.preset_picker.get() == 'Custom', 'editing marks options as Custom')
            box.close(force=True)
            check(app._toolbox is None, 'closing disconnects the toolbox')
            app.open_toolbox()
            box = app._toolbox
            check(box.options.crop == edited.crop and box.options.effect == 'grayscale', 'reopen retains applied editing choices')
            until(lambda: box._image is not None and box._rendered_key is not None)
            box.split_var.set('.75')
            box.split()
            until(lambda: app._toolbox is None and not app._building_rows)
            check(len(app.items) == 2 and abs(app.items[0].clip_seconds - .75) < .001 and abs(app.items[1].clip_seconds - 1.25) < .001,
                  'split creates adjacent trim ranges in order')
            check(app.items[0].path == source and app.items[1].path == source, 'split preserves original source reference')
            app.settings.mode = 'separate'
            app.settings.boomerang = False
            app._start()
            until(lambda: not app.busy, 40)
            check(len(app.results) == 2, 'split segments export as two separate GIFs')
            check(all(r.size <= r.cap_bytes and r.size <= engine.HARD_CAP_BYTES for r in app.results),
                  'cropped reversed watermarked exports retain measured size caps')
            check(all(r.width < r.height for r in app.results), 'percentage crop changes exported aspect')
            app.open_toolbox()
            box = app._toolbox
            until(lambda: box._image is not None and box._rendered_key is not None)
            box.start_var.set('0')
            box.end_var.set('.6')
            box.fps_var.set('5')
            box.size_var.set('320 px')
            output = work / 'frames.zip'
            box.extract(output)
            until(lambda: not box._exporting and output.exists(), 30)
            with ZipFile(output) as archive:
                check(archive.namelist() == ['frame0001.png', 'frame0002.png', 'frame0003.png'],
                      'toolbox exports anonymous ordered PNG frames')
            box.effect_var.set('Sepia')
            box._draft_changed()
            with patch('yui.toolbox.ask_confirmation', return_value=False):
                app._on_close()
            check(not app._closed and not box._closed, 'declining discard preserves app and media-tools draft')
            box.close(force=True)
            app._reset_options()
            check(app.settings.crop is None and app.settings.effect == 'none' and not app.settings.reverse,
                  'Reset all options clears edits')
            check(hashlib.sha256(source.read_bytes()).hexdigest() == original, 'all operations preserve original bytes')
            check(not errors, 'application workflow has no Tk callback exceptions')
        finally:
            if app._toolbox is not None:
                app._toolbox.close(force=True)
            app._on_close()
    report = {'ok': True, 'count': len(checks), 'checks': checks, 'native_tk': True,
              'actual_gif_and_frame_exports': True, 'accounts_accessed': False}
    (work / 'editing-integration-test.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
