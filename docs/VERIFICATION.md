# Source snapshot verification

This source snapshot was checked against the published Yui 1.6.0 EXE before
publication. All 19 application files (`app.py` and the 18 `yui` package files)
compile to structurally equal Python code objects to those stored in the EXE.
Only code-object source filenames were normalized for this comparison.

This confirms the application module correspondence. It is not a complete audit
of the EXE, all bundled dependencies or its native loader, and it is not a
bit-for-bit reproducible build claim. Review code before running it.

`SOURCE_MANIFEST.json` records the application comparison results and each
included file's SHA-256. It excludes its own hash. The published binary ZIP and
EXE hashes identify the release used for the comparison.

The snapshot checks also compile all included Python files and the PyInstaller
spec, verify the component source archive and the five upstream source hashes,
and scan selected text and artwork metadata for private local identifiers and
credential markers. These targeted checks are not a guarantee that code is safe.

Build instructions and public documentation were updated for this source
release. The application modules remain unchanged from the published EXE.

## Verify the checked-out files

Run `python tools/verify_source.py` from the repository folder to check the
recorded file hashes without running the app. Use Python 3.14 and install the
pinned build requirements for the optional `--exe` comparison. The helper first
checks the supplied EXE's hash, then reads its PyInstaller archives as data.
It never launches that executable.

The original release tag was created before source publication. Its automatic
GitHub source archives describe that older tag; the full source is on `main`.
Documentation, this verification helper, and Git attributes were added or
updated for repository publication. The 19 application files are unchanged.
