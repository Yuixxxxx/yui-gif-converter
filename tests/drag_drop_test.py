"""Load real tkdnd and exercise its Windows file-drop callback bridge."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from PIL import Image
from yui import TERMS_VERSION, engine
from yui.paths import get_ffmpeg
from yui.settings import Settings
from yui.ui import App, MAX_DROP_DATA_CHARS


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    run = work / uuid.uuid4().hex
    run.mkdir()
    os.environ['APPDATA'] = str(run / 'profile')
    os.environ['LOCALAPPDATA'] = str(run / 'local')
    image = run / 'image with spaces {猫}.png'
    video = run / 'video with spaces {犬}.mp4'
    extra = run / '{extra image}.jpg'
    Image.new('RGB', (64, 48), 'pink').save(image)
    Image.new('RGB', (80, 60), 'purple').save(extra)
    subprocess.run([get_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
                    '-i', 'testsrc2=s=64x48:r=15:d=2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
                   check=True, capture_output=True, creationflags=engine.CREATE_NO_WINDOW)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (image, video, extra)}
    checks = []
    errors = []
    def check(ok, name):
        assert ok, name
        checks.append(name)
    def pump(condition):
        until = time.monotonic() + 12
        while not condition() and time.monotonic() < until:
            app.root.update()
            time.sleep(.004)
        check(condition(), 'pending import finishes')
    with patch.object(Settings, 'load', return_value=Settings(accepted_terms=TERMS_VERSION, sound=False)):
        app = App()
        app.root.withdraw()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        try:
            check(bool(app.dnd_files and app.root.TkdndVersion), 'real Tcl 9 Windows drag backend loads')
            check('win-x64-tcl9' in str(app.root.tk.call('set', 'tkdnd::_package_dir')), 'correct Tcl 9 binary is selected')
            target = app.drop_badge._canvas
            for event in ('<<DropEnter>>', '<<DropPosition>>', '<<DropLeave>>', '<<Drop>>'):
                check(bool(app.root.tk.call('bind', str(target), event)), 'drop badge binds ' + event)
            def native_drop(widget, paths):
                data = app.root.tk.call('list', *(str(p) for p in paths))
                action = app.root.tk.call('::tkdnd::olednd::HandleDragEnter', str(widget), ('CF_HDROP',),
                                          ('copy', 'move'), (), 100, 100, (), data)
                check(action == 'copy' and app.stage.cget('border_width') == 2, 'native enter highlights and accepts copy')
                action = app.root.tk.call('::tkdnd::olednd::HandleDragOver', str(widget), ('Shift',), 100, 100)
                check(action == 'copy', 'Shift drag cannot request moving original files')
                action = app.root.tk.call('::tkdnd::olednd::HandleDrop', str(widget), (), 100, 100, 'CF_HDROP', data)
                check(action == 'copy' and app.stage.cget('border_width') == 1, 'drop resets border and reports copy')
                app.root.tk.call('::tkdnd::olednd::HandleDragLeave', str(widget))
            native_drop(target, (image, video))
            pump(lambda: app.loading == 0 and not app._building_rows)
            check([item.path for item in app.items] == [image, video], 'mixed image/video filenames retain spaces braces Unicode and order')
            check([item.kind for item in app.items] == ['image', 'video'], 'dropped image and video are probed correctly')
            check(abs(app.items[1].duration - 2) < .1, 'dropped video has its full duration')
            native_drop(app.rows[0].thumb._canvas, (extra,))
            pump(lambda: app.loading == 0 and not app._building_rows)
            check([item.path for item in app.items] == [image, video, extra], 'new media can be dropped on a dynamically created row')
            native_drop(app.stage._canvas, (image,))
            pump(lambda: app.loading == 0)
            check(len(app.items) == 3, 'dropping the same file again does not duplicate it')
            with patch.object(app, 'add_paths') as add:
                app.busy = True
                check(app._on_drop(SimpleNamespace(data=str(image), actions=('copy',))) == 'refuse_drop' and not add.called,
                      'conversion rejects new drops')
                app.busy = False
                remote = app.root.tk.call('format', '%s', app.root.tk.call('list', r'\\server\share\photo.png'))
                for data in ('{broken Tcl list', 'x' * (MAX_DROP_DATA_CHARS + 1), remote):
                    check(app._on_drop(SimpleNamespace(data=data, actions=('copy',))) == 'refuse_drop' and not add.called,
                          'malformed oversized or remote drop is rejected without importing')
                check(app._on_drop(SimpleNamespace(data=str(image), actions=('move',))) == 'refuse_drop', 'move-only source is rejected')
                app.settings.accepted_terms = ''
                check(app._on_drop(SimpleNamespace(data=str(image), actions=('copy',))) == 'refuse_drop', 'first-run gate cannot be bypassed by a drop')
                app.settings.accepted_terms = TERMS_VERSION
            check(all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items()), 'all originals remain byte-identical')
            check(not errors, 'no Tk callbacks fail')
        finally:
            app._on_close()
    report = {'ok': True, 'count': len(checks), 'checks': checks,
              'inspection': 'real Tcl 9 backend, Windows CF_HDROP callback bridge, real file probing; physical Explorer drag tested separately'}
    (work / 'drag-drop-test.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
