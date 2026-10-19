"""Verify the runtime, or extract its exact pinned bytes from the released app.

This development helper never executes the supplied EXE or downloads anything.
Corresponding source and rebuild instructions ship in the full ZIP.
"""
from pathlib import Path
import argparse
import hashlib
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui.runtime import FFMPEG_SHA256

def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from-exe', type=Path, help='Released YuiGifConverter.exe to parse without executing')
    args = parser.parse_args()
    target = ROOT / 'bin/ffmpeg.exe'
    if target.is_file() and digest(target) == FFMPEG_SHA256:
        print('Pinned FFmpeg integrity verified.')
        return
    if args.from_exe is None:
        raise SystemExit('Pinned runtime missing or changed. Use --from-exe with the released app, or follow docs/DEVELOPMENT.md to build and pin a reviewed runtime.')
    from PyInstaller.archive.readers import CArchiveReader
    app = CArchiveReader(str(args.from_exe))
    members = [name for name in app.toc if name.replace('\\', '/') == 'bin/ffmpeg.exe']
    if len(members) != 1 or app.toc[members[0]][2] > 180 * 1024 * 1024:
        raise SystemExit('The app does not contain one bounded FFmpeg runtime.')
    payload = app.extract(members[0])
    if hashlib.sha256(payload).hexdigest() != FFMPEG_SHA256:
        raise SystemExit('Runtime checksum mismatch; extraction stopped.')
    target.parent.mkdir(exist_ok=True)
    temporary = target.with_suffix('.tmp')
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print('Extracted and verified the released FFmpeg runtime.')

if __name__ == '__main__':
    main()
