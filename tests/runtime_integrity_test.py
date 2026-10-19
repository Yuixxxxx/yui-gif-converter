"""Verify converter overrides and altered binaries cannot bypass the pinned runtime."""

from pathlib import Path
import json
import os
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui import paths
from yui.runtime import FFMPEG_VERSION


def main():
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    executable = paths.get_ffmpeg()
    checks = []
    fake = work / 'ffmpeg.exe'
    fake.write_bytes(b'altered decoder; never executed')
    with patch.dict(os.environ, {'IMAGEIO_FFMPEG_EXE': str(fake), 'FFMPEG_BINARY': str(fake), 'PATH': str(work)}):
        assert paths.get_ffmpeg() == executable
        checks.append('runtime ignores executable overrides and PATH')
    with patch.object(paths, 'resource', return_value=fake):
        try:
            paths.get_ffmpeg()
        except RuntimeError as error:
            assert str(work) not in str(error)
            checks.append('altered converter fails checksum before execution with a private error')
        else:
            raise AssertionError('Altered decoder was accepted')
    with patch.object(paths, 'resource', return_value=work/'missing.exe'):
        try:
            paths.get_ffmpeg()
        except RuntimeError:
            checks.append('missing converter cannot fall back to external software')
        else:
            raise AssertionError('Missing decoder was accepted')
    assert paths.get_ffmpeg() == executable
    checks.append('trusted converter remains available after rejected overrides')
    result = {'ok': True, 'version': FFMPEG_VERSION, 'count': len(checks), 'checks': checks}
    (work/'runtime-integrity-test.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
