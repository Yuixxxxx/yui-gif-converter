"""Export the source archives carried in the application's standard ZIP overlay."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable
from zipfile import ZipFile

from .paths import is_frozen, is_local_path, resource

MAX_SOURCE_BYTES = 64 * 1024 * 1024
SOURCE_FILES = {
    'ffmpeg': 'FFmpeg-corresponding-source.zip',
}

class SourceExportError(ValueError):
    """A recoverable source export failure, without personal paths."""

class SourceExportCancelled(SourceExportError):
    pass

def filename(kind: str = 'ffmpeg') -> str:
    try:
        return SOURCE_FILES[kind]
    except KeyError:
        raise SourceExportError('Only FFmpeg component source is included.') from None

@contextmanager
def _source_stream(kind: str):
    name = filename(kind)
    if is_frozen():
        app = Path(sys.executable)
        if not is_local_path(app):
            raise SourceExportError('Run Yui from a local drive to save its source.')
        with ZipFile(app) as package:
            info = package.getinfo('SOURCE_MANIFEST.json')
            if info.file_size > 16384:
                raise SourceExportError('The source information is damaged. Download Yui again.')
            manifest = json.loads(package.read(info))
            entry = manifest['sources'][kind]
            info = package.getinfo('Sources/' + name)
            if info.file_size != entry['size_bytes'] or not 0 < info.file_size <= MAX_SOURCE_BYTES:
                raise SourceExportError('The bundled source is damaged. Download Yui again.')
            with package.open(info) as source:
                yield source, entry
    else:
        manifest = json.loads(resource('vendor', 'source-manifest.json').read_text(encoding='utf-8'))
        entry = manifest['sources'][kind]
        source_path = resource('vendor', name)
        if not is_local_path(source_path) or not 0 < source_path.stat().st_size <= MAX_SOURCE_BYTES:
            raise SourceExportError('The bundled source is unavailable. Prepare the release again.')
        with source_path.open('rb') as source:
            yield source, entry

def export_source(output: Path, kind: str = 'ffmpeg', *, overwrite: bool = False,
                  cancelled: Callable[[], bool] = lambda: False) -> Path:
    """Publish verified source bytes atomically on a checked local destination."""
    filename(kind)
    output = Path(output)
    if not is_local_path(output):
        raise SourceExportError('Choose a folder on a local drive for the source ZIP.')
    if output.suffix.lower() != '.zip':
        raise SourceExportError('Choose a filename ending in .zip.')
    if cancelled():
        raise SourceExportCancelled('Source export cancelled.')
    staging = None
    try:
        if not is_frozen():
            original = resource('vendor', filename(kind))
            if output.absolute() == original.absolute() or (output.exists() and original.samefile(output)):
                raise SourceExportError('Save the source ZIP to a different location.')
        if output.exists() and not overwrite:
            raise SourceExportError('That file already exists. Choose a different name.')
        with _source_stream(kind) as (source, entry):
            output.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.yui-source-', suffix='.tmp', delete=False) as target:
                staging = Path(target.name)
                digest = hashlib.sha256()
                size = 0
                while block := source.read(1024 * 1024):
                    if cancelled():
                        raise SourceExportCancelled('Source export cancelled.')
                    size += len(block)
                    if size > MAX_SOURCE_BYTES:
                        raise SourceExportError('The bundled source is damaged. Download Yui again.')
                    digest.update(block)
                    target.write(block)
                if size != entry['size_bytes'] or digest.hexdigest() != entry['sha256']:
                    raise SourceExportError('Source integrity check failed. Download Yui again.')
                target.flush()
                os.fsync(target.fileno())
        if cancelled():
            raise SourceExportCancelled('Source export cancelled.')
        if not is_local_path(output):
            raise SourceExportError('The destination changed. Choose a local folder and retry.')
        if overwrite:
            os.replace(staging, output)
        elif os.name == 'nt':
            os.rename(staging, output)
        else:
            os.link(staging, output)
        return output
    except SourceExportError:
        raise
    except Exception as exc:
        raise SourceExportError('Could not save the source ZIP. Check the folder, free space, or download Yui again.') from exc
    finally:
        if staging is not None:
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                pass
