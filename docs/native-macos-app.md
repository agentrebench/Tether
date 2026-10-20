# Tether macOS terminal

The native app opens a login shell immediately. Agent selection is optional and
is available from **Agent** in the toolbar or Shell menu. **Monitor** opens the
bundled btop; `btop` is also on the shell PATH. Neither a provider account nor a
model server is needed to use the terminal.

## Everyday use

- **⌘T** creates a terminal tab; **⌘D** splits right. The split menu also splits
  down. Click a pane to focus it. **⌘W** closes the focused pane; each tab has a
  close button. Shell history, completion, job control, Ctrl+C, terminal editors,
  and fullscreen programs run through a real PTY.
- **⌘,** or the gear opens Settings. Search installed Oh My Zsh themes and the
  built-in prompts. **Preview theme** creates a disposable live shell without
  changing preferences. **Use theme** saves that selection. **Save & open**
  saves the selected shell/theme/glyph mode and creates a terminal. Existing
  panes and jobs stay running. Custom themes accept a local file path.
- Font family and text size are app preferences. A Fira Code Nerd Font is
  bundled, including Powerline glyphs, with Menlo/Monaco and installed mono
  fonts also selectable. No system font installation is needed.
- The footer folder button chooses the starting directory for new panes. It
  never sends a `cd` command into a running program.
- The thinking light appears while an agent is making a model request or
  receiving reasoning events. Click it to score; **Catch the light** is also
  keyboard accessible. It clears during tool execution, user approvals,
  completion, cancellation, and agent exit. Disable it in Settings or use its
  close button. macOS Reduce Motion stops its movement. Terminal keys continue
  going to the terminal.

Existing `tether`, `tether chat`, `tether theme list`, and theme commands remain
available. The app shares the existing shell preferences but does not rewrite
`.zshrc`, `.zprofile`, or global shell configuration.

## Implementation boundary

| Component | Implementation |
| --- | --- |
| Agent loop, providers, tools, sessions, codebase knowledge, theme discovery | Existing Python |
| PTYs, bounded queues, child supervision, cancellation, activity/game state | C23 |
| macOS window, menus, WebKit lifecycle, directory chooser | Small Objective-C Cocoa adapter |
| Terminal emulation and display | Existing xterm.js dependency and local HTML/CSS/JavaScript |

The native app does not use the legacy Tauri/Rust host. That source remains
available under `desktop/`; this change does not discard working Python or
rewrite the renderer. btop is an unmodified third-party C++ executable.
This is the native terminal foundation, not the durable DAG scheduler/journal
described in the longer-term runtime mission. No speedup claims are made.

The Python boundary is documented in [native-python-protocol.md](native-python-protocol.md).
Only the activity phase and monotonic sequence cross the OSC metadata channel;
private reasoning content does not. PTY lifecycle and limits are documented in
[native/README-runtime.md](../native/README-runtime.md). Rendering acknowledges
output after xterm consumes it; C limits outstanding output to 256 KiB per pane.
The UI allows 14 panes, leaving two runtime slots for settings jobs. Scrollback
is bounded to 5,000 lines per pane. Native navigation is restricted to bundled
local content, and terminal output is never interpreted as host JavaScript.

## Build

This packaging target is **Apple Silicon macOS 26 or later**, matching the
pinned btop bottle. Other architecture/OS packages need an appropriate btop
build and a reviewed deployment target; do not reuse this bottle for them.
The resulting app embeds the Python executable and needs no Python, Node,
Homebrew, or network connection to open a shell or btop on the target Mac.

Build dependencies: modern Apple Clang (tested with 21), CMake 3.25+, Python
with `.[bundle,dev]`, and the locked npm packages in `desktop/`. Ninja is used
when available; Unix Makefiles is the documented fallback.

```sh
cd desktop && npm ci && cd ..
python scripts/build_macos_app.py --fetch
open dist/macos/Tether.app
```

`--fetch` downloads missing assets. The font, licenses, and btop archive are
pinned and SHA-256 checked. Later offline builds omit `--fetch`.
`--reuse-engine` is intended only after rebuilding `dist/tether` from the
current Python source. Bundled licenses and btop's source URL/SBOM are in
`Contents/Resources/licenses`.

The bundle is locally ad-hoc signed and verified. Distribution to other Macs
still requires the usual Developer ID signing/notarization workflow; this
build script does not publish or release anything.

### Disk image installer

From the repository root, build a fresh app and local installer with:

```sh
bash desktop/scripts/package_macos.sh --adhoc
```

The equivalent command from `desktop/` is `npm run dmg -- --adhoc`. Native
terminal packaging is the default; `--legacy` explicitly selects the older
Tauri desktop app. `--fetch` allows missing pinned native assets to download.
The packager uses `.venv/bin/python`, falling back to `python3`; set
`TETHER_BUILD_PYTHON` to choose another build environment.

The version comes from the built app's Info.plist. For version 0.4.0 the outputs
are `dist/macos/Tether-0.4.0.dmg` and `Tether-0.4.0.dmg.sha256`. Open the DMG and
drag **Tether** onto **Applications**. It contains the bundled Python engine,
btop, terminal renderer, font, and licenses. Apple Silicon macOS 26+ is required.

Packaging verifies the image, mounts it read-only, checks the app's signature
and version, compares its executables with the build, and runs both bundled
helpers before replacing the output DMG. Earlier versions remain available.
Verify the downloaded image from its directory with
`shasum -a 256 -c Tether-0.4.0.dmg.sha256`.

`--adhoc` creates a local build without accessing signing credentials or sending
anything to Apple's notarization service. Omit it to use the existing Developer
ID signing/notarization flow when configured. An ad-hoc build is not notarized
for distribution to other Macs. Packaging does not upload a GitHub release or
replace the installed application.

```sh
python -m pytest -q
cmake -S native -B build/native/sanitize -DCMAKE_BUILD_TYPE=Debug \
  -DTETHER_SANITIZERS=ON -DTETHER_MACOS_APP=OFF
cmake --build build/native/sanitize
ctest --test-dir build/native/sanitize --output-on-failure
```

The app has an opt-in integration harness: set `TETHER_APP_SMOKE_REPORT` to an
absolute writable JSON report path when launching its executable. Use a private
`TETHER_CONFIG_DIR`, `ZDOTDIR`, and `XDG_CONFIG_HOME`. The test exercises real
shell I/O, Ctrl+C, split resizing, theme discovery/preview, bundled btop and
agent startup, and light activation/scoring/cleanup. It makes no model calls;
activity input is explicitly simulated and Python event mapping is tested
separately. The app writes the report plus a WebKit snapshot and exits.
