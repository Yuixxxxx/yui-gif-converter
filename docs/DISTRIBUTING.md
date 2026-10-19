# Release contents

The Windows download contains only `YuiGifConverter.exe`. Its readable ZIP
overlay includes component source, notices and the user guide. The EXE can be
read with a standard ZIP reader without being executed. Component source can
also be saved from Settings → Licenses → FFmpeg source.

The Git repository's main branch contains Yui's application source, tests,
artwork and build scripts, together with the FFmpeg corresponding-source
package. A separate release ZIP retains the original source snapshot. The included third-party archive is preserved
unchanged and its binary pin matches the 1.6.0 runtime.

The application's existing terms are in `LICENSE.txt` and `legal/TERMS.txt`.
Publishing readable source does not select a new open-source license for Yui.
Third-party components retain their own licenses and notices. Preserve the
corresponding-source materials and component notices when sharing the app.

Local virtual environments, build output, user media, settings, logs, credentials
and project history are not part of the public source snapshot.

The released app is unsigned. A checksum detects changed bytes but does not
authenticate the publisher or guarantee the app is safe.
