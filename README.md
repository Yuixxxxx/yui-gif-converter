# Yui GIF Converter

Free, offline GIF conversion and editing for Windows 10/11 (64-bit).

This repository contains the actual Yui **1.6.0 application source**, tests,
build tools, artwork and component notices. Browse `app.py` and `yui/` directly
on GitHub, or clone the repository to inspect and build it locally.

## Get the code

```sh
git clone https://github.com/Yuixxxxx/yui-gif-converter.git
cd yui-gif-converter
```

Start with [app.py](app.py), the [application modules](yui), and the
[build script](build.ps1). Follow [Development](docs/DEVELOPMENT.md) to run the
app from source or build an EXE. The build uses Windows x64, Python 3.14.8 or a
newer Python 3.14 maintenance version, and the pinned requirements files.

## Download the Windows app

Get **YuiGifConverter-1.6.0-full-package.zip** from the
[1.6.0 release](https://github.com/Yuixxxxx/yui-gif-converter/releases/tag/v1.6.0),
extract it, and open `YuiGifConverter.exe`. A separate Python installation is
not needed for that download. The portable app is unsigned.

## Features

- Convert videos or images into GIFs; combine files or export separately.
- Trim, resize, crop, adjust speed, reverse, add effects, and control palettes.
- Add text, stickers and watermarks, or export a still PNG poster.
- Split clips, extract frames, and choose a measured output limit up to 10 MiB.

See the [User guide](docs/USER_GUIDE.md) for usage and local data locations.

## Inspect and verify

All 19 application files compile to the same Python code objects as those
embedded in the published 1.6.0 EXE, after normalizing source filenames.
[Verification](docs/VERIFICATION.md) explains the comparison and its limits.
`SOURCE_MANIFEST.json` records individual source hashes and the binary used.

```powershell
python tools\verify_source.py
# Optional, using Python 3.14 and the build requirements:
python tools\verify_source.py --exe .\YuiGifConverter.exe
```

The optional comparison parses the EXE as data; it does not execute it.
This is not a complete binary audit or a bit-for-bit reproducible build claim.
The original `v1.6.0` tag predates the source commit; browse or clone `main` for
the complete source. The release also retains a source ZIP as a snapshot.

## Privacy and terms

Conversion happens locally and requires no account or media upload. This
repository excludes private media, logs, local settings, credentials, virtual
environments and previous build output. The included assets and creator links
are intentional public branding:
[Buy me a matcha on Ko-fi](https://ko-fi.com/yui_xxx) and
[Yui on JuicyChat](https://www.juicychat.ai/userdetailspace/1933409514837725186).

Copyright © 2026 Yui. Existing application terms remain in [LICENSE.txt](LICENSE.txt).
Publishing the source does not select a new open-source license. Components
retain their own licenses and notices in `legal/` and `vendor/`.
