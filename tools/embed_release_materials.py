"""Prepare release source and append a readable standard ZIP to the Windows app.

The overlay can be read with ordinary ZIP tools without running Yui. PyInstaller
keeps its original archive/cookie intact; the overlay is not unpacked on startup.
"""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
import hashlib
import json
import os
import shutil
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui import VERSION, legal
from yui.runtime import FFMPEG_SHA256

def prepare():
    runtime = ROOT / 'vendor/FFmpeg-corresponding-source.zip'
    with ZipFile(runtime) as package:
        assert package.testzip() is None
        manifest = json.loads(package.read('BUILD.json'))
        assert manifest['ffmpeg_exe_sha256'] == FFMPEG_SHA256
        assert len(manifest['source_archives']) == 5
        for item in manifest['source_archives']:
            assert item['file'] == Path(item['file']).name
            assert hashlib.sha256(package.read('archives/' + item['file'])).hexdigest() == item['sha256']
    sources = {}
    for kind, path in (('ffmpeg', runtime),):
        sources[kind] = {'file': path.name, 'size_bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest = {'app_version': VERSION, 'sources': sources, 'format': 'Standard ZIP overlay; read without executing the app or save source through Settings > Licenses.'}
    (ROOT / 'vendor/source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    overlay = ROOT / 'vendor/source-payload.zip'
    with ZipFile(overlay, 'w', compression=ZIP_DEFLATED, compresslevel=9) as package:
        package.write(runtime, 'Sources/' + runtime.name, compress_type=ZIP_STORED)
        package.writestr('SOURCE_MANIFEST.json', json.dumps(manifest, indent=2))
        package.write(ROOT / 'README.md', 'README.md')
        for path in sorted((ROOT / 'legal').rglob('*.txt')):
            if path.name in legal.DOCUMENTS.values():
                key = next(key for key, value in legal.DOCUMENTS.items() if value == path.name)
                package.writestr(path.relative_to(ROOT).as_posix(), legal.document(key))
            else:
                package.write(path, path.relative_to(ROOT).as_posix())
    print('Prepared matching FFmpeg component source and notices for the binary download.')

def embed():
    from PyInstaller.archive.readers import CArchiveReader
    exe = ROOT / 'dist/YuiGifConverter.exe'
    overlay = ROOT / 'vendor/source-payload.zip'
    archive = CArchiveReader(str(exe))
    assert archive._COOKIE_MAGIC_PATTERN not in overlay.read_bytes(), 'Overlay conflicts with executable archive marker'
    # Preserve all original executable bytes up through the PyInstaller cookie;
    # replace any prior overlay, making repeated build invocations safe.
    base_end = archive._end_offset
    temporary = exe.with_suffix('.embedded.tmp')
    try:
        with exe.open('rb') as source, temporary.open('wb') as target:
            left = base_end
            while left:
                block = source.read(min(left, 1024 * 1024))
                if not block:
                    raise ValueError('Incomplete executable archive')
                target.write(block)
                left -= len(block)
            with overlay.open('rb') as materials:
                shutil.copyfileobj(materials, target, 1024 * 1024)
        os.replace(temporary, exe)
    finally:
        temporary.unlink(missing_ok=True)
    with ZipFile(exe) as package:
        assert package.testzip() is None
        assert [name for name in package.namelist() if name.startswith('Sources/')] == ['Sources/FFmpeg-corresponding-source.zip']
    assert 'python314.dll' in CArchiveReader(str(exe)).toc
    print('Embedded all release materials inside YuiGifConverter.exe.')

if __name__ == '__main__':
    if sys.argv[1:] == ['prepare']:
        prepare()
    elif sys.argv[1:] == ['embed']:
        embed()
    else:
        raise SystemExit('Use prepare before PyInstaller, then embed after PyInstaller.')
