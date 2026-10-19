"""Package the self-contained EXE with no external documentation or source files."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui import VERSION

def main():
    exe = ROOT / 'dist/YuiGifConverter.exe'
    with ZipFile(exe) as embedded:
        assert embedded.testzip() is None
        manifest = json.loads(embedded.read('SOURCE_MANIFEST.json'))
        assert manifest['app_version'] == VERSION
        assert set(manifest['sources']) == {'ffmpeg'}, 'Only required component source may be distributed'
        assert [name for name in embedded.namelist() if name.startswith('Sources/')] == ['Sources/FFmpeg-corresponding-source.zip']
        assert not any(name.startswith(('yui/', 'tools/', 'tests/', 'docs/')) or name.endswith(('.py', '.spec', '.ps1')) for name in embedded.namelist()), 'Private application files cannot be packaged'
        for kind, item in manifest['sources'].items():
            assert hashlib.sha256(embedded.read('Sources/' + item['file'])).hexdigest() == item['sha256']
        assert 'legal/THIRD_PARTY_NOTICES.txt' in embedded.namelist()
    archive = exe.with_name(f'YuiGifConverter-{VERSION}-win64.zip')
    with ZipFile(archive, 'w', compression=ZIP_DEFLATED, compresslevel=9) as package:
        package.write(exe, exe.name)
    public_archive = exe.with_name(f'YuiGifConverter-{VERSION}-full-package.zip')
    shutil.copyfile(archive, public_archive)
    print(f'Public upload: {public_archive.name} ({archive.stat().st_size:,} bytes)')
    print('ZIP contents: YuiGifConverter.exe only. Required component source is embedded; app source is excluded.')

if __name__ == '__main__':
    main()
