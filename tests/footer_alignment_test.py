"""Check the painted button content layout at common Windows display scales."""
from pathlib import Path
import json
import os
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from yui import TERMS_VERSION
from yui.settings import Settings
from yui.ui import App

work = Path(sys.argv[1]).resolve()
work.mkdir(parents=True, exist_ok=True)
os.environ['APPDATA'] = str(work / 'profile')
os.environ['LOCALAPPDATA'] = str(work / 'local')
checks = []
errors = []

def check(condition, name):
    assert condition, name
    checks.append(name)

def hidden_root(app):
    app.dnd_files = None
    root = ctk.CTk()
    root.withdraw()
    return root

with patch.object(App, '_make_root', hidden_root), patch.object(Settings, 'load', return_value=Settings(
        accepted_terms=TERMS_VERSION, sound=False)):
    app = App()
    app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
    app.root.geometry('1120x740+10000+10000')
    app.root.deiconify()
    try:
        for theme in ('dark', 'light'):
            ctk.set_appearance_mode(theme)
            for scale in (1, 1.25, 1.5, 2):
                ctk.set_widget_scaling(scale)
                app.root.geometry(f'{round(1120 * scale)}x{round(740 * scale)}+10000+10000')
                app.root.update()
                for name, button in (('Ko-fi', app.support_button), ('JuicyChat', app.profile_button)):
                    text, image = button._text_label, button._image_label
                    check(text is not None and image is not None, f'{theme} {scale}x {name}: image and readable label are present')
                    left = min(text.winfo_x(), image.winfo_x())
                    right = max(text.winfo_x() + text.winfo_width(), image.winfo_x() + image.winfo_width())
                    check(abs((left + right) / 2 - button.winfo_width() / 2) <= 1.5,
                          f'{theme} {scale}x {name}: icon and text group is horizontally centered')
                    for label in (text, image):
                        check(abs(label.winfo_y() + label.winfo_height() / 2 - button.winfo_height() / 2) <= 1.5,
                              f'{theme} {scale}x {name}: content is vertically centered')
                    check(text.winfo_width() >= text.winfo_reqwidth() and image.winfo_width() >= image.winfo_reqwidth(),
                          f'{theme} {scale}x {name}: text and icon are not clipped')
                    check(left >= 0 and right <= button.winfo_width(), f'{theme} {scale}x {name}: content fits its button')
                check(app.profile_button.winfo_x() == app.support_button.winfo_x()
                      and app.profile_button.winfo_width() == app.support_button.winfo_width()
                      and app.profile_button.winfo_height() == app.support_button.winfo_height(),
                      f'{theme} {scale}x: stacked buttons share aligned edges and dimensions')
        check(not errors, 'theme and scale changes produce no Tk callback errors')
    finally:
        app._on_close()
        ctk.set_widget_scaling(1)
report = {'ok': True, 'count': len(checks), 'checks': checks,
          'inspection': 'actual Tk widget geometry in an isolated off-screen window; both themes at 100%, 125%, 150%, 200%'}
(work / 'footer-alignment-test.json').write_text(json.dumps(report, indent=2))
print(json.dumps({'ok': True, 'count': len(checks), 'inspection': report['inspection']}))
