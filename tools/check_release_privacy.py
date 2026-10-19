"""Verify Yui attribution and reject personal builder details in a release."""

import json
import marshal
import os
import sys
import types
import hashlib
import io
from pathlib import Path
from zipfile import ZipFile

import pefile
from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from yui import VERSION
from yui.runtime import FFMPEG_SHA256

exe = ROOT / "dist" / "YuiGifConverter.exe"
bundle = ROOT / "dist" / f"YuiGifConverter-{VERSION}-win64.zip"
previous_name = sys.argv[1] if len(sys.argv) > 1 else Path(os.environ.get("USERPROFILE", "")).name
denied = [previous_name.lower().encode(), previous_name.lower().encode("utf-16-le")] if previous_name else []


def clean(data, label):
    assert not any(value in data.lower() for value in denied), f"Personal attribution found in {label}"


def check_code(code, label):
    assert not (":\\users\\" in code.co_filename.lower() or ":/users/" in code.co_filename.lower()), label
    clean(marshal.dumps(code), label)
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            check_code(value, label)


def main():
    clean(exe.read_bytes(), "EXE")
    with ZipFile(exe) as materials:
        assert materials.testzip() is None
        assert 'legal/TERMS.txt' in materials.namelist()
        assert 'legal/THIRD_PARTY_NOTICES.txt' in materials.namelist()
        manifest = json.loads(materials.read('SOURCE_MANIFEST.json'))
        assert manifest['app_version'] == VERSION
        assert set(manifest['sources']) == {'ffmpeg'}
        assert [name for name in materials.namelist() if name.startswith('Sources/')] == ['Sources/FFmpeg-corresponding-source.zip']
        assert not any(name.startswith(('yui/', 'tools/', 'tests/', 'docs/')) or name.endswith(('.py', '.ps1', '.spec')) for name in materials.namelist())
        for item in manifest['sources'].values():
            payload = materials.read('Sources/' + item['file'])
            assert len(payload) == item['size_bytes']
            assert hashlib.sha256(payload).hexdigest() == item['sha256']
            with ZipFile(io.BytesIO(payload)) as sources:
                assert sources.testzip() is None
                for name in sources.namelist():
                    clean(name.encode(), 'Embedded source name')
                    clean(sources.read(name), name)
        with ZipFile(io.BytesIO(materials.read('Sources/FFmpeg-corresponding-source.zip'))) as sources:
            runtime = json.loads(sources.read('BUILD.json'))
            assert runtime['ffmpeg_exe_sha256'] == FFMPEG_SHA256
            for item in runtime['source_archives']:
                assert hashlib.sha256(sources.read('archives/' + item['file'])).hexdigest() == item['sha256']
    archive = CArchiveReader(str(exe))
    dnd_files = [name.replace('\\', '/') for name in archive.toc if 'tkinterdnd2/tkdnd/' in name.replace('\\', '/')]
    assert dnd_files and all('/win-x64-tcl9/' in name for name in dnd_files)
    assert any(name.endswith('libtkdnd2.10.1.dll') for name in dnd_files)
    assert not any(name.replace('\\', '/').startswith(('yui/', 'tools/', 'tests/', 'docs/')) or name == 'app.py' or name.endswith('-source.zip') for name in archive.toc)
    assert 'python314.dll' in archive.toc
    assert 'yui.overlays' in archive.open_embedded_archive("PYZ.pyz").toc
    assert 'yui.designer' in archive.open_embedded_archive("PYZ.pyz").toc
    assert all(name in archive.open_embedded_archive("PYZ.pyz").toc for name in ('yui.transforms', 'yui.media_tools', 'yui.toolbox'))
    assert hashlib.sha256(archive.extract('bin/ffmpeg.exe' if 'bin/ffmpeg.exe' in archive.toc else 'bin\\ffmpeg.exe')).hexdigest() == FFMPEG_SHA256
    for name, toc in archive.toc.items():
        payload = archive.extract(name)
        clean(payload, name)
        if toc[-1] == "s":
            check_code(marshal.loads(payload), name)
    pyz = archive.open_embedded_archive("PYZ.pyz")
    assert not any(name.startswith('imageio_ffmpeg') for name in pyz.toc)
    for name in pyz.toc:
        code = pyz.extract(name)
        if isinstance(code, types.CodeType):
            check_code(code, name)
    pe = pefile.PE(str(exe))
    strings = {}
    for group in pe.FileInfo:
        for info in group:
            for table in getattr(info, "StringTable", []):
                strings.update({key.decode(): value.decode() for key, value in table.entries.items()})
    assert strings["CompanyName"] == "Yui"
    assert strings["LegalCopyright"] == "© 2026 Yui"
    assert strings["ProductVersion"] == VERSION + ".0"
    with ZipFile(bundle) as download:
        assert download.namelist() == ['YuiGifConverter.exe'], 'Public ZIP must contain only the EXE'
        assert download.read('YuiGifConverter.exe') == exe.read_bytes()
    for release, required in ((bundle, "YuiGifConverter.exe"),):
        with ZipFile(release) as zipped:
            assert zipped.testzip() is None
            for name in zipped.namelist():
                clean(name.encode(), "ZIP filenames")
                clean(zipped.read(name), name)
            assert required in zipped.namelist()
            assert not any("settings.json" in name or "presets.json" in name or "error.log" in name or ".venv/" in name for name in zipped.namelist())
    selftest = json.loads((ROOT / "dist" / "selftest.json").read_text())
    assert selftest["ok"] and selftest["privacy"]["network_protocols_blocked"]
    assert selftest["duration_regression"]["ok"]
    assert all(selftest["features"].values())
    assert all(selftest['embedded_source_exports'].values())
    assert set(selftest['embedded_source_exports']) == {'ffmpeg'}
    assert selftest['drag_and_drop']['native_backend_loaded_and_target_registered']
    assert selftest['design_editor']['ok']
    assert selftest['design_editor']['native_editor_preview']
    assert selftest['design_editor']['text_and_sticker_gif']
    assert selftest['design_editor']['metadata_free_png_poster']
    assert selftest['themed_confirmations']['ok']
    assert selftest['source_menu']['ok']
    assert selftest['editing_tools']['ok']
    clean(json.dumps(selftest).encode(), "selftest report")
    report = {"ok": True, "publisher": strings["CompanyName"], "copyright": strings["LegalCopyright"],
              "personal_attribution_absent": True, "builder_profile_paths_absent": True,
              "personal_settings_or_logs_in_zip": False, "zip_integrity_passed": True,
              "component_source_privacy_and_integrity_passed": True,
              "application_source_not_bundled": True, "private_build_files_not_bundled": True,
              "frozen_network_protocol_test_passed": True, "frozen_duration_test_passed": True,
              "frozen_presets_and_save_copy_tests_passed": True,
              "pinned_ffmpeg_integrity_passed": True, "python314_bundled": True,
              "imageio_runtime_dependency_removed": True,
              "public_zip_contains_only_exe": True, "legal_and_source_inside_exe": True,
              "embedded_sources_export_and_integrity_passed": True}
    report['frozen_drag_backend_loaded_and_registered'] = True
    report['frozen_design_editor_and_png_poster_passed'] = True
    report['frozen_themed_confirmation_dialog_passed'] = True
    report['frozen_component_source_menu_passed'] = True
    report['frozen_editing_and_watermark_tools_passed'] = True
    (ROOT / "dist" / "privacy-release-check.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
