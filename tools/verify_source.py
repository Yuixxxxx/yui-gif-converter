"""Verify source hashes and optionally compare the published EXE without executing it."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import marshal
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parent.parent

def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()

def normalize(code):
    if isinstance(code, types.CodeType):
        return code.replace(co_filename='', co_consts=tuple(normalize(x) for x in code.co_consts))
    return code

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, help='Official 1.6.0 EXE to read as data; never executed')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
    for item in manifest['files']:
        relative = PurePosixPath(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise SystemExit('Invalid manifest path.')
        path = ROOT.joinpath(*relative.parts)
        if not path.is_file() or path.stat().st_size != item['size_bytes'] or digest(path) != item['sha256']:
            raise SystemExit('Source file differs: ' + item['path'])
    print('Verified source hashes: ' + str(len(manifest['files'])) + ' files.')
    if args.exe is None:
        return
    if sys.version_info[:2] != (3, 14):
        raise SystemExit('Use Python 3.14 for the released bytecode comparison.')
    if digest(args.exe) != manifest['executable']['sha256']:
        raise SystemExit('EXE checksum differs from the recorded 1.6.0 release.')
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
    archive = CArchiveReader(str(args.exe))
    with tempfile.TemporaryDirectory(prefix='yui-source-verify-') as temporary:
        pyz_path = Path(temporary) / 'application.pyz'
        pyz_path.write_bytes(archive.extract('PYZ.pyz'))
        pyz = ZlibArchiveReader(str(pyz_path))
        files = manifest['application_code_comparison']['files']
        for item in files:
            relative = item['file']
            if relative == 'app.py':
                embedded = marshal.loads(archive.extract('app'))
            else:
                module = 'yui' if relative == 'yui/__init__.py' else relative[:-3].replace('/', '.')
                embedded = pyz.extract(module)
            compiled = compile((ROOT / relative).read_bytes(), relative, 'exec')
            if normalize(embedded) != normalize(compiled):
                raise SystemExit('Application module differs: ' + relative)
        print('Application modules match the recorded EXE: ' + str(len(files)) + '.')
    print('This comparison does not establish full binary reproducibility or security.')

if __name__ == '__main__':
    main()
