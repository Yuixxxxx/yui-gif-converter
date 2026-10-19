# Run and build from source

## Requirements

- Windows 10/11, 64-bit Intel or AMD.
- 64-bit Python 3.14.8 or a newer Python 3.14 maintenance version, with tkinter.
- PowerShell and access to PyPI when installing the pinned dependencies.

Clone the repository, enter its folder, and open PowerShell there:

```powershell
python -m venv .venv314
.\.venv314\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
```

The runtime dependencies and build tool are pinned in those requirement files.
The app itself does not download tools or updates at runtime.

## Provide FFmpeg

The repository includes `vendor/FFmpeg-corresponding-source.zip`, containing
the FFmpeg and enabled-library sources, patch, build scripts and tool references.
It does not include an FFmpeg executable.

For the exact released runtime, place the extracted official
`YuiGifConverter.exe` in this source folder and run:

```powershell
.\.venv314\Scripts\python.exe tools\fetch_ffmpeg.py --from-exe .\YuiGifConverter.exe
```

This helper parses the PyInstaller archive without executing the supplied EXE.
It extracts only `bin/ffmpeg.exe` and checks its SHA-256 against
`yui/runtime.py` before accepting it. Inspect the helper before running it.

To build FFmpeg independently instead, extract the corresponding-source ZIP
to its own folder and follow its README. Review your compiled binary and copy it
to `bin/ffmpeg.exe`. Locally compiled bytes can differ, so update the reviewed
checksum in `yui/runtime.py` and the matching corresponding-source `BUILD.json`
together. The packager verifies that they agree. The component README refers
to an earlier Yui version; its recorded runtime hash is also used by 1.6.0.

## Run the application

```powershell
.\.venv314\Scripts\python.exe app.py
```

The first run presents the bundled terms. Normal app settings and outputs use
the locations described in the user guide. Files passed as command-line
arguments are added to the conversion list.

## Build the Windows download

```powershell
.\build.ps1
# To include the full regression suite:
.\build.ps1 -RunSmokeTests
```

If Python 3.14 is not the default, pass its executable using `-Python`.
PowerShell execution policy must permit your reviewed local script; use your
normal system policy rather than changing it for an unreviewed script.

The build installs pinned dependencies, verifies FFmpeg, regenerates artwork,
prepares component source and notices, then packages with PyInstaller. It runs
the frozen EXE's self-test and privacy/package checks before producing:

- `dist/YuiGifConverter.exe`
- `dist/YuiGifConverter-1.6.0-full-package.zip`
- `dist/YuiGifConverter-1.6.0-win64.zip`
- `dist/selftest.json` and `dist/SHA256SUMS.txt`

The binary download contains one complete EXE; the public application source is
provided separately. Do not infer that a rebuilt EXE must match the published
EXE's hash. Toolchains, timestamps, fonts and packaging can change binary bytes.

## Selected checks

Each script creates synthetic test files in the supplied work directory. Some
UI tests open app windows. Review the scripts before running them.

```powershell
.\.venv314\Scripts\python.exe tests\runtime_integrity_test.py work\runtime-check
.\.venv314\Scripts\python.exe tests\privacy_test.py work\privacy-check
.\.venv314\Scripts\python.exe tests\security_paths_test.py work\security-paths
.\.venv314\Scripts\python.exe tests\security_media_test.py work\security-media
.\.venv314\Scripts\python.exe tests\security_integration_test.py work\security-integration
.\.venv314\Scripts\python.exe tests\smoke_test.py work\smoke
```

`build.ps1 -RunSmokeTests` lists and runs the release regression checks. Test
success does not substitute for reviewing your changes or testing real inputs.
