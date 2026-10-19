# Yui Gif Converter

Free, offline video and image conversion for Windows 10/11 (64-bit). Make one GIF
from several files, or a separate GIF for each. Outputs stay under your chosen
size limit, up to 10 MB (10 MiB). No account, uploads or tracking.

## Get started

1. Extract `YuiGifConverter-1.6.0-full-package.zip` and open `YuiGifConverter.exe`.
2. Review the terms and privacy notice on first launch.
3. Drag images, videos, mixed selections or a media folder from Explorer into Yui.
   You can also click the media panel to browse. Dropping never moves originals.
4. Adjust trims, image durations and conversion options. Open **Text & stickers / poster…**
   to add captions and image stickers, then choose **Apply to GIF**.
5. Choose **Create GIF**, preview the result, **Save as** a new name or location, or **Copy GIF**.

The designer includes draggable layers, size, rotation, opacity, text fonts/colours,
bold/outline, layer ordering, duplication, Undo/Redo and numeric or arrow-key placement.
Transparent PNG and WebP stickers retain transparency. Animated stickers use their first frame.
Designs cover every frame of the GIF and use the same relative placement for each GIF in a batch.
Drafts stay in memory for this session; **Start over** clears them. Keep source stickers available
until conversion completes. Reopening the designer uses the original media, so it does not stack
the design twice. Its preview shows the first source clip; the overlay applies to the full combined GIF.

**Crop, effects & frame tools…** provides a preview and percentage crop controls,
Grayscale, Sepia, Vivid, Warm, Cool and Invert effects, reverse playback, a palette
color limit and palette analysis modes. These edits apply to every clip in the next
GIF export and are included in saved conversion presets. **Reset all options** clears them.
Resizing, speed, frame rate and the measured output-size limit remain in the main panel.

Choose a source in the tools window to split its current trim into two editable segments.
The split time is relative to the beginning of the trimmed clip; neither segment alters
the original file. Use **Separate GIFs** to export the two segments individually.
Frame extraction saves anonymous PNG files in one ZIP. Choose a source time range,
sample rate and frame size; frame exports are limited to 300 frames per archive.
Extraction samples the selected source range before GIF speed, boomerang and
palette reduction. It includes crop, effects and static design layers; Reverse
changes the order of the exported frames. Temporary extraction space is bounded.

In the designer, **Text watermark** and **Image watermark** create translucent layers.
Replace the default text or select your own local logo, choose a corner, and adjust
size or opacity. Watermarks cover the whole GIF and are also included in poster exports.
Watermark text and image paths stay in the current session; they are not saved in presets.

For a still design, use the frame slider and choose **Save poster PNG** in the designer.
PNG posters can use a 1600 or 2048 px long side. Saving a poster does not apply an unfinished GIF
draft, change source media, or copy camera metadata. GIF exports retain the chosen size cap;
PNG posters use their selected resolution.

Confirmation prompts follow the selected theme. **Keep editing**, Escape, or closing
the confirmation preserves an unfinished design; **Discard changes** closes it.

Python and a separate FFmpeg installation are not needed. This portable app is
unsigned; Windows may display an unrecognized-app warning.

## Useful controls

- Presets: Balanced, Small file, Smooth motion and Sticker; save your own options.
- Editing: trim, speed, boomerang, transitions, fit/fill and background colour.
- Preview: Pause, Resume and Restart.
- Shortcuts: **Ctrl+O** adds files, **Ctrl+Enter** converts, **Ctrl+S** saves a copy.
- **Escape** cancels an import/conversion or returns from the preview.
- Settings contains appearance, output folder, completion options, About and legal texts.

Version 1.4 adds anonymous export filenames (enabled by default), duplicate-import
skipping and a 200-item list limit. Conversion runs with time, memory and
temporary-space limits; oversized media shows an error with a recovery step.
FFmpeg 9.0.2 with a purpose-built offline configuration, Python 3.14.8 and Tcl/Tk 9.0.4 are bundled. Large file lists
stay responsive, and preference changes save in the background. Terms and privacy pages are shorter; original third-party
licenses remain available in Settings → Licenses.

## Files and privacy

GIFs default to `%LOCALAPPDATA%\YuiGifConverter\GIFs`. Settings, saved presets and
error logs stay in `%APPDATA%\YuiGifConverter`. Yui preserves source files and
removes source metadata from exported GIFs. Processing requires local files.

Choosing a cloud-synced output folder lets that separate service sync your files.
Copy GIF sets Windows clipboard history and cloud-sync exclusion flags. Other
clipboard tools may behave differently. Yui does not sign in to these services.
The creator links and avatar are intentional public branding; visible media
content can still identify people. See the Privacy page in Settings for details.

Yui is free to use. **Buy me a matcha**, with the Ko-fi cup logo, opens
[Ko-fi](https://ko-fi.com/yui_xxx) for optional support when clicked.
The purple **Yui on JuicyChat**
button below it opens the creator profile. Its avatar is bundled locally; both
links open in your browser only when clicked.

## If something goes wrong

Use **Cancel** or **Cancel import** to stop. Completed GIFs are kept. If saving
fails, choose a writable local folder with free space. Close a GIF in other
programs before replacing it. Unknown-duration videos may take longer to import.
For sites using a decimal 10 MB limit, choose the app's 8 MB preset.

Version 1.6.0 downloads contain only `YuiGifConverter.exe`. The notices, this
guide and the required FFmpeg component source are inside the app. Yui's
application source, tests and build files are available in the public Git
repository at https://github.com/Yuixxxxx/yui-gif-converter (main branch).
Read notices in Settings → Licenses; select **FFmpeg source** in the dropdown,
then **Save FFmpeg source** to save an archive only when needed. Sharing the complete EXE preserves
all bundled materials; there are no companion files to keep beside it.

The EXE also carries a standard ZIP archive containing the source and notices.
ZIP readers such as Python's `zipfile` can read that archive without running Yui.
Command-line source export is available with `YuiGifConverter.exe --export-source
"C:\LocalFolder\FFmpeg-source.zip"`.
No Python, development tools, account or internet connection is needed to use the app.

Yui application code is copyrighted separately from its third-party components.
