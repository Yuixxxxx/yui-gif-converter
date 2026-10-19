"""New edit options survive local settings and presets without image paths."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    os.environ['APPDATA'] = str(work / 'profile')
    os.environ['LOCALAPPDATA'] = str(work / 'local')
    from yui.settings import Settings
    from yui.presets import PresetStore
    from yui.transforms import validate_edit_options
    checks = []

    def check(value, name):
        assert value, name
        checks.append(name)

    custom = Settings(crop=(.1, .2, .6, .7), effect='sepia', reverse=True,
                      palette_colors=32, palette_mode='full', speed=1.5, max_side=640)
    options = validate_edit_options(custom.render_options())
    check(options.crop == (.1, .2, .6, .7) and options.effect == 'sepia' and options.reverse,
          'settings transfer crop effect and reverse to renderer')
    check(options.palette_colors == 32 and options.palette_mode == 'full', 'palette choices transfer to renderer')
    custom.save()
    loaded = Settings.load()
    check(asdict(loaded) == asdict(custom), 'editing choices reload from a local JSON settings file')
    presets = PresetStore(work / 'presets.json')
    presets.save_current('Edited', custom, overwrite=True)
    restarted = PresetStore(presets.path)
    target = Settings(theme='light', private_filenames=True, output_dir='unchanged')
    restarted.apply(target, 'Edited')
    check(target.crop == custom.crop and target.effect == 'sepia' and target.reverse,
          'editing choices survive preset restart and application')
    check(target.palette_colors == 32 and target.palette_mode == 'full', 'palette choices survive preset application')
    check(target.theme == 'light' and target.output_dir == 'unchanged' and target.private_filenames,
          'edit preset preserves workflow and privacy')
    check(not any(key in json.loads(presets.path.read_text())['presets']['Edited'] for key in ('overlays', 'image', 'watermark')),
          'saved presets contain no watermark text or sticker file paths')
    target.reset_options()
    check(target.crop is None and target.effect == 'none' and not target.reverse and target.palette_colors == 256
          and target.palette_mode == 'auto', 'reset clears every new edit option')
    old = work / 'legacy-presets.json'
    old.write_text(json.dumps({'version': 1, 'presets': {'Old': {'fps': 15}}}))
    legacy = PresetStore(old).get('Old')
    check(legacy['crop'] is None and legacy['effect'] == 'none' and not legacy['reverse'] and legacy['palette_colors'] == 256,
          'older presets inherit safe editing defaults')
    for bad in ([0, 0, 2, 1], [0, 0, float('nan'), 1], [True, 0, 1, 1], '../private.png', [0, 0, 1]):
        check(Settings(crop=bad).sanitize().crop is None, 'hostile crop setting recovers to full canvas')
    for field, value, expected in (('effect', 'arbitrary,filter', 'none'), ('reverse', 'yes', False),
                                   ('palette_mode', {}, 'auto'), ('palette_colors', float('nan'), 256)):
        setting = Settings(**{field: value}).sanitize()
        check(getattr(setting, field) == expected, 'hostile edit preference recovers: ' + field)
    check(Settings(crop=[.1, .2, .6, .7]).sanitize().crop == (.1, .2, .6, .7), 'JSON crop list normalizes to immutable tuple')
    report = {'ok': True, 'count': len(checks), 'checks': checks}
    (work / 'editing-settings-test.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
