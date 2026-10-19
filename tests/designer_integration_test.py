"""Exercise the actual app-to-designer-to-GIF workflow with synthetic media."""
from pathlib import Path
import hashlib
import json
import os
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PIL import Image, ImageDraw
import customtkinter as ctk
from yui import TERMS_VERSION, engine
from yui.overlays import OverlayLayer
from yui.settings import Settings
from yui.ui import App

work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
os.environ['APPDATA'] = str(work / 'profile')
os.environ['LOCALAPPDATA'] = str(work / 'local')
source = work / 'source.png'
Image.new('RGB', (240, 180), '#162b48').save(source)
sticker = work / 'sticker.png'
mark = Image.new('RGBA', (48, 48))
ImageDraw.Draw(mark).ellipse((1, 1, 46, 46), fill='#ff00ff')
mark.save(sticker)
before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, sticker)}
settings = Settings(accepted_terms=TERMS_VERSION, sound=False, output_dir=str(work / 'exports'), max_side=240)
checks = []
errors = []
def check(value, name):
    assert value, name
    checks.append(name)
with patch.object(Settings, 'load', return_value=settings):
    app = App()
app.root.report_callback_exception = lambda *args: errors.append(str(args[1]))
app.root.geometry('1120x740+12000+12000')
def spin(seconds=.03):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.root.update()
        time.sleep(.003)
def until(predicate):
    deadline = time.monotonic() + 25
    while not predicate():
        assert time.monotonic() < deadline, 'Integration timed out: ' + app.status.cget('text')
        spin()
try:
    check(app.design_button.cget('state') == 'disabled', 'designer is disabled until media exists')
    app.add_paths([source])
    until(lambda: not app.loading and not app._building_rows)
    check(app.design_button.cget('state') == 'normal', 'import enables design without blocking the UI')
    app.open_designer()
    editor = app._designer
    editor.geometry('1060x760+12000+12000')
    until(lambda: not editor._loading and editor._rendered_layers == editor.layers)
    check(editor.source.path == source and editor.protected_paths == (source, source), 'editor receives original source and original-file protection')
    spin(.15)
    check(editor.grab_current() is editor, 'editor uses a local modal grab to protect parent edits')
    app.open_designer()
    check(app._designer is editor, 'reopening raises the existing editor instead of starting a second draft')
    editor.add_text()
    editor._property_changed('text', 'Yui poster')
    editor._property_changed('y', .15)
    editor.add_image(sticker)
    until(lambda: len(editor.layers) == 2 and editor._rendered_layers == editor.layers)
    editor._property_changed('x', .75)
    editor._property_changed('y', .7)
    until(lambda: editor._rendered_layers == editor.layers)
    poster = work / 'poster.png'
    editor.save_poster(poster, overwrite=True)
    until(lambda: not editor._exporting)
    check(poster.is_file() and not app.design_layers, 'explicit poster export saves a PNG without silently applying the GIF draft')
    editor.apply()
    check(app._designer is None and len(app.design_layers) == 2 and app.view == 'list', 'Apply transfers both layers and returns to Create GIF')
    check('2 design layers' in app.summary.cget('text'), 'app shows the active design count')
    app.on_primary()
    check(app.busy and app.design_button.cget('state') == 'disabled', 'conversion snapshots the design and disables competing edits')
    until(lambda: not app.busy)
    check(len(app.results) == 1 and app.view == 'result', 'actual decorated GIF export reaches the result preview')
    result = app.results[0]
    with Image.open(result.output) as gif:
        pixel = gif.convert('RGB').getpixel((round(gif.width * .75), round(gif.height * .7)))
        check(pixel[0] > 230 and pixel[1] < 35 and pixel[2] > 230, 'exported GIF contains the sticker at the reviewed normalized position')
    check(result.size <= settings.render_options().cap_bytes, 'decorated GIF retains the chosen size cap')
    app.open_designer()
    editor = app._designer
    editor.geometry('1060x760+12000+12000')
    until(lambda: not editor._loading and editor._rendered_layers == editor.layers)
    check(editor.source.path == source and editor.layers == app.design_layers, 'result design reopens the original without stacking baked overlays')
    editor.add_text()
    with patch('yui.designer.ask_confirmation', return_value=False):
        app._on_close()
    check(not app._closed and not editor._closed, 'declining to discard keeps both app and unsaved editor open')
    editor.close(force=True)
    app.clear_items()
    check(not app.design_layers and app.design_button.cget('state') == 'disabled', 'Start over clears session-only text and stickers')
    check(all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items()), 'all original media and sticker bytes remain unchanged')
    check(not errors, 'app/editor workflow produces no Tk callback exceptions')
finally:
    if app._designer is not None:
        app._designer.close(force=True)
    app._on_close()
report = {'ok': True, 'count': len(checks), 'checks': checks, 'real_gif_and_poster_export': True, 'user_accounts_touched': False}
(work / 'designer-integration-test.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
