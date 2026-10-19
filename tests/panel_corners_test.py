"""Verify content never covers the panel's rounded border at different scales."""
import json
import math
import os
from pathlib import Path
import sys
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import customtkinter as ctk
from yui import TERMS_VERSION, engine
from yui.settings import Settings
from yui.ui import App


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    profile = work / uuid.uuid4().hex
    os.environ['APPDATA'] = str(profile / 'profile')
    os.environ['LOCALAPPDATA'] = str(profile / 'local')
    checks = []
    errors = []
    def check(condition, description):
        assert condition, description
        checks.append(description)
    def hidden_root(app):
        app.dnd_files = None
        root = ctk.CTk()
        root.withdraw()
        return root
    with patch.object(App, '_make_root', hidden_root), patch.object(Settings, 'load', return_value=Settings(
            accepted_terms=TERMS_VERSION, sound=False)):
        app = App()
        app.root.report_callback_exception = lambda *args: errors.append(type(args[1]).__name__)
        app.items = [engine.MediaItem(work / 'sample.mp4', 'video', 480, 720, duration=5, fps=25)]
        app.root.geometry('1120x740+10000+10000')
        app.root.deiconify()
        app.root.update()
        try:
            for theme in ('dark', 'light'):
                ctk.set_appearance_mode(theme)
                for scale in (1, 1.25, 1.5, 2):
                    ctk.set_widget_scaling(scale)
                    app.root.geometry(f'{round(1120 * scale)}x{round(740 * scale)}+10000+10000')
                    for border in (1, 2):
                        app.stage.configure(border_width=border)
                        for name, view in (('empty', app.empty_view), ('list', app.list_view), ('result', app.result_view)):
                            app._show_view(name)
                            app.root.update_idletasks()
                            # Tk uses real pixel coordinates while CTk properties
                            # use logical units. Test the physical rounded arc.
                            actual_scale = app.stage._get_widget_scaling()
                            radius = round(app.stage.cget('corner_radius') * actual_scale)
                            stroke = round(border * actual_scale)
                            left = view.winfo_x()
                            top = view.winfo_y()
                            right = app.stage.winfo_width() - view.winfo_x() - view.winfo_width()
                            bottom = app.stage.winfo_height() - view.winfo_y() - view.winfo_height()
                            for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
                                distance = math.hypot(max(0, radius - x), max(0, radius - y))
                                check(distance <= radius - stroke,
                                      f'{theme} {scale}x {border}px {name}: content clears rounded border at ({x},{y})')
                            check(view.winfo_manager() == 'grid', f'{theme} {scale}x {name}: selected view stays visible')
            check(not errors, 'theme, scale and view changes produce no Tk errors')
        finally:
            app._on_close()
            ctk.set_widget_scaling(1)
    report = {'ok': True, 'count': len(checks), 'checks': checks,
              'inspection': 'isolated Tk geometry; verifies all panel views at 100%, 125%, 150%, 200% in both themes'}
    (work / 'panel-corners-test.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'ok': True, 'count': len(checks), 'inspection': report['inspection']}))


if __name__ == '__main__':
    main()
