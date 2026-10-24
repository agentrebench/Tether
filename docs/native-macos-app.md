# Tether macOS terminal

The native app opens a login shell immediately. Agent selection is optional and
is available from **Agent** in the toolbar or Shell menu. **Monitor** opens the
bundled btop; `btop` is also on the shell PATH. Neither a provider account nor a
model server is needed to use the terminal.

## Everyday use

- **Agent** opens the original provider picker with the Tether logo and moving
  light. After choosing a provider, chat shows a small session heading, a compact
  input that grows up to eight rows, and a quiet model/effort/context caption.
  Enter sends; Alt+Enter adds a newline. Slash completion, history, cancellation,
  and approvals retain their existing behavior. Chat colors follow the app palette.

- **Terminal (+ / ⌘T)**, **Agent**, and **Monitor** open resizable panes in the
  current session. Terminals and agents split beside the focused pane when it is
  wide, or below it when it is narrow. Monitor docks below the session to give
  its dashboard more room. **⌘D** explicitly splits right; the split menu
  also splits down. Drag a divider to resize. Each pane has a title, **□** to
  maximize/restore, and **×** to close only that pane. Double-click its title to
  maximize/restore. Opening another pane restores the split layout first.
  Right-click a pane or click **···** in its title bar for **Move left/right/up/down**;
  these place the running pane at that edge of the session. Drag a title bar
  onto another pane's edge to dock beside it; the highlighted region previews
  the destination. Moving preserves the process, scrollback, and unfinished input.
  During divider dragging the text grid stays stable; it fits the new space on
  release. App-window resizing settles for 120 ms before resizing the PTY.
  Click a pane to focus it; **⌘W** closes the focused pane. The session's top
  close button closes all of its panes. Shell history, completion, job control, Ctrl+C, terminal editors,
  and fullscreen programs run through a real PTY.
  In short panes the Agent picker keeps its logo and moving light while trimming
  secondary text. btop's full dashboard needs at least 80 columns × 24 rows;
  maximize its pane or enlarge the app window if it shows a size message.
- **⌘,** or the gear opens Settings. Search 142 included Oh My Zsh themes, your
  installed/custom themes, and the
  built-in prompts. **Preview theme** creates a disposable live shell pane without
  changing preferences. **Use theme** saves that selection. **Save & open**
  saves the selected shell/theme/glyph mode and creates a terminal pane. Existing
  panes and jobs stay running. Custom themes accept a local file path.
  The included collection works offline on a fresh install; local themes with
  the same names take precedence. agnoster, robbyrussell, af-magic, bira, and avit
  are included. Choosing a zsh theme automatically selects zsh for new panes.
- **Settings → App color theme** changes the entire workbench immediately:
  Tether Dark, VS Code Dark/Light, Gruvbox Dark/Light, Dracula, and Solarized Dark.
  These are built-in palettes, independent of zsh prompt themes. Chrome, file
  previews, native window appearance, and running terminals update together;
  app theme selection persists without opening or restarting a shell. Importing
  VS Code extension/theme files is not currently supported.
- Drag the divider beside Explorer, above the terminal, or between any nested
  terminal splits to resize panes. The position menu in the file preview header
  docks the file **Top**, **Right**, **Bottom**, or **Left** of the terminal and
  remembers the choice. Moving the preview keeps shells and file contents intact. Focus a divider and use arrow keys for small
  adjustments, Home/End for its limits, or double-click to reset. Explorer width
  and preview height are remembered; terminal split sizes last for that tab.
- Font family and text size are app preferences. A Fira Code Nerd Font is
  bundled, including Powerline glyphs, with Menlo/Monaco and installed mono
  fonts also selectable. No system font installation is needed.
- The footer folder button chooses the starting directory for new panes. It
  never sends a `cd` command into a running program.
- File browsing starts closed and does not list folders until requested.
  **Files** or **⌘B** opens the Explorer sidebar; pressing it again closes the
  sidebar and any file preview, giving the space back to the terminal.
  Explorer's **···** opens a menu with Open folder, Refresh files, Show hidden
  files, Collapse folders, and Close Files. Escape dismisses the menu.
  Click a folder to expand it,
  or a file to open its read-only preview above the terminal. Arrow keys navigate
  the tree; Right expands or enters a folder, Left collapses or returns to its
  parent, and Enter opens the selection. The preview supports text selection,
  copying, line numbers, scrolling, and syntax highlighting for Python, C/C++,
  JavaScript/TypeScript, shell, HTML/CSS, JSON, Rust, Go, and other source files.
  Language labels/icons use filenames; extensionless scripts also use shebangs.
  Unknown files fall back to plain text; its **×** closes it. Escape returns to
  the shell (and closes the preview when focused there).
- Source paths in terminal output (such as `src/main.py:12:3`) are clickable
  and open the file preview. Relative paths resolve from the selected Explorer
  root; absolute paths must be inside it. Paths outside that root, parent
  traversal, and symbolic links remain blocked. Plain terminal output retains
  the colors supplied by the running program.
- The terminal scrollbar is hidden until the pointer reaches its right edge;
  its hit area remains available, and its thumb can be dragged through scrollback.
  Explorer and file-preview scrollbars also appear on hover. Fullscreen CLI
  applications can own their own scrollback; the terminal bar represents only
  the history those applications leave in the terminal buffer.
- Running the native `claude` or `codex` executable in a shell pane automatically
  enables conversation colors. Recognized assistant prose uses the app accent
  (green in Tether Dark); human questions use the text color. Explicit ANSI
  error, diff, and syntax colors remain intact. This is a display-only heuristic
  based on the CLI's visible speaker markers, not access to model message roles.
  Unknown layouts and continuations more than 240 rows from a speaker marker
  may retain their original colors. Per-pane **··· → Conversation colors…**
  selects Automatic, Claude Code, Codex, or Original CLI colors. Choose a CLI
  explicitly for shell wrappers, Node launchers, or remote sessions that cannot
  be identified from the local foreground process. No CLI config is rewritten.
- Explorer's folder button chooses a root; **↻** refreshes and **···** opens
  the Explorer action menu, including Show hidden files. **F5** also refreshes while the tree has focus. The root is shared
  across terminal tabs and sets the starting directory for new shells. Refresh
  after files change; the explorer does not watch the filesystem. Binary and
  non-UTF-8 files show an explanation instead of a text preview. Symbolic links
  are displayed but not followed; choose their target as the root to browse it.
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
| Lazy local directory listings and bounded file reads | C23, off the UI thread |
| macOS window, menus, WebKit lifecycle, directory chooser | Small Objective-C Cocoa adapter |
| Terminal emulation and display | Existing xterm.js dependency and local HTML/CSS/JavaScript |

The native app does not use the legacy Tauri/Rust host. That source remains
available under `desktop/`; this change does not discard working Python or
rewrite the renderer. btop is an unmodified third-party C++ executable.
This is the native terminal foundation, not the durable DAG scheduler/journal
described in the longer-term runtime mission. No speedup claims are made.

The Python boundary is documented in [native-python-protocol.md](native-python-protocol.md).
Activity phase, monotonic sequence, and compact-composer resize readiness cross
the OSC metadata channel; private reasoning content does not. PTY lifecycle and limits are documented in
[native/README-runtime.md](../native/README-runtime.md). Rendering acknowledges
output after xterm consumes it; C limits outstanding output to 256 KiB per pane.
The UI allows 14 panes, leaving two runtime slots for settings jobs. Scrollback
is bounded to 5,000 lines per pane. Native navigation is restricted to bundled
local content, and terminal output is never interpreted as host JavaScript.

File browsing uses an independent serial queue (at most eight pending host
requests), so it does not consume terminal process slots. Reads stay within the
selected root using descriptor-relative paths without following symlinks.
Listings stop at 500 visible entries or 5,000 examined entries per directory;
the display caps its cache at 128 folders, depth at 30, and visible rows at 2,000.
Previews stop at 256,000 bytes or 2,000 lines. The locked highlight.js dependency
runs in a cancellable worker; highlighting is limited to 64,000 characters and
2 seconds, with plain-text fallback. Generated markup is reconstructed using
only text nodes and span classes; file content cannot create executable DOM.
Tree selection reuses existing rows, and terminal fitting is coalesced to one
animation frame. Delayed
responses cannot replace a newer selection or reopen a closed preview.

## Build

This packaging target is **Apple Silicon macOS 26 or later**, matching the
pinned btop bottle. Other architecture/OS packages need an appropriate btop
build and a reviewed deployment target; do not reuse this bottle for them.
The resulting app embeds the Python executable and its unpacked runtime, and needs no Python, Node,
Homebrew, or network connection to open a shell or btop on the target Mac.
The app uses PyInstaller's directory layout so opening a terminal, Agent tab,
or settings does not extract Python libraries into a temporary directory each
time. The standalone CLI download remains a single executable.

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
`--reuse-engine` is intended only after running `python scripts/build_binary.py
--onedir` to rebuild `dist/native-engine/tether/` from current Python source.
The app packages the entire directory, including `_internal` and framework
symlinks. `python scripts/build_binary.py` still builds the standalone CLI at
`dist/tether`. Bundled licenses and btop's source URL/SBOM are in
`Contents/Resources/licenses`.

By default the bundle is locally ad-hoc signed and verified. Setting
`APPLE_SIGNING_IDENTITY` signs the app and embedded libraries with that identity.
Use the distribution packaging workflow below to notarize it for other Macs;
the app build script does not notarize, publish, or release anything.

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

The version comes from the built app's Info.plist. For version 0.4.1 the outputs
are `dist/macos/Tether-0.4.1.dmg` and `Tether-0.4.1.dmg.sha256`. Open the DMG and
drag **Tether** onto **Applications**. It contains the bundled Python engine,
btop, terminal renderer, font, and licenses. Apple Silicon macOS 26+ is required.

Packaging verifies the image, mounts it read-only, checks the app's signature
and version, compares its executables with the build, and runs both bundled
helpers before replacing the output DMG. Earlier versions remain available.
Verify the downloaded image from its directory with
`shasum -a 256 -c Tether-0.4.1.dmg.sha256`.

`--adhoc` creates a local build without accessing signing credentials or sending
anything to Apple's notarization service. Omit it to use the existing Developer
ID signing/notarization flow when configured. An ad-hoc build is not notarized
for distribution to other Macs. Packaging does not upload a GitHub release or
replace the installed application.

Test an existing app's bundled engine and web assets without substituting
development code (the harness adds test instrumentation to a temporary copy):

```sh
.venv/bin/python scripts/test_native_explorer.py --build dist/macos --packaged \
  --startup --output build/release-check/startup
.venv/bin/python scripts/test_native_explorer.py --build dist/macos --packaged \
  --panes --output build/release-check/panes
```

Each output directory must be new. For a mounted installer, set `--build` to
the volume containing `Tether.app`. The harness checks the source bundle's
signature first, uses isolated shell settings, and makes no model requests.

For a distribution build, configure the `tether-notary` Keychain profile with
`xcrun notarytool store-credentials tether-notary` using its secure interactive
prompts (keep passwords out of shell arguments and environment variables), then
run `bash desktop/scripts/package_macos.sh` without `--adhoc`. The packager
discovers a Developer ID Application identity (or uses `APPLE_SIGNING_IDENTITY`)
and passes it to PyInstaller so the embedded Python libraries are signed before
being archived. The app and helpers use the hardened runtime and secure
timestamps. After Apple's acceptance, the packager staples both the app and
DMG and checks Gatekeeper. `TETHER_NOTARY_PROFILE` selects a different existing
Keychain profile. If Apple returns HTTP 403 about an agreement, the team's
Account Holder must review the pending agreement in their Apple Developer
account before notarization can continue.

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

For the Explorer alone, a development harness stages a temporary app with the
current Python environment and locked xterm assets. It opens a real macOS window
and zsh PTY using fixture files and isolated shell/config directories. It does
not need bundled btop, a frozen engine, or a provider account:

```sh
python scripts/test_native_explorer.py --build build/native/sanitize \
  --output /tmp/tether-explorer-wide
python scripts/test_native_explorer.py --build build/native/sanitize \
  --output /tmp/tether-explorer-small --small
```

Build with `TETHER_MACOS_APP=ON` for these tests. Output directories must be new.
Reports include native reads, inert HTML, hidden files, keyboard browsing,
binary/large files, a running shell beside the preview, and delayed-response
races. Each run saves `report.json.explorer.png` for visual review.

## Workbench checks

Run `node --test native/tests/workbench_unit.cjs` after installing the locked
desktop dependencies. `python scripts/test_native_explorer.py --build
build/native/release --engine dist/native-engine/tether --workbench --output <new-directory>`
opens an isolated test app, exercises real zsh PTYs, syntax highlighting,
keyboard/pointer-event resizing, nested pane removal, and app themes. Add
`--small` for a 680 × 440 viewport. Pointer geometry uses synthetic events with
capture stubbed; normal native pointer capture is used in the shipping app.
The JSON report records five samples of 80 arrow-key selections through a
350-file fixture. This is a tree navigation measurement, not an overall app
performance benchmark.

The agent layout is checked with `--agent-ui` in the same native test harness.
It follows the actual native Agent launch, original picker, Codex selection,
and compact REPL, with an isolated executable that only answers `login status`.
It checks the moving light, input height, multiline paste, slash completion,
local commands, light/dark palettes, and clean exit. `--engine dist/native-engine/tether`
exercises this route in the frozen engine as well. No model requests are made;
live provider responses are outside this display test.

For repeatable pane startup measurements, run:

```sh
python scripts/test_native_explorer.py --startup \
  --engine dist/native-engine/tether --output /tmp/tether-startup
```

The report contains five sequential samples each for shell and Agent panes,
from the open request to a visible prompt or provider picker in xterm, plus
native pane creation time, median and maximum latency. It also verifies shell
input and clean pane exit. The shell uses isolated startup files; user shell
plugins and provider authentication may add latency outside this measurement.
`--engine` also accepts a single executable for comparisons with older builds.

`--resize` runs divider regression checks through Cocoa mouse events delivered
into WebKit, including native hit testing and pointer capture. It covers the
expanded grab area, horizontal/vertical nested panes, Explorer and docked file
previews, and continued dragging after capture is released. These checks use
Cocoa-injected events, not a physical mouse; they do not stub pointer capture.
They also verify that Files starts closed and that its button closes both the
browser and preview. The host accepts test mouse input only with the explicit
`TETHER_APP_SMOKE_REPORT` test environment enabled.

`--pane-controls` exercises native Cocoa right-clicks and title-bar drags,
movement of existing Terminal/Agent/Monitor processes to all four edges, and
Explorer menu actions. An isolated Codex authentication fixture enters the real
compact REPL without making a model request. It checks that repeated divider
resizes cause one grid change per release, retain the draft without ghost
copies, preserve command input, and keep the grid inside its visible box. Run
with `--small` as well to check the minimum window size. Python handshake bounds,
duplicate preparation, cancellation, and opt-in behavior are covered by
`tests/test_native_resize.py`.

`--conversation` checks native foreground-process detection with an isolated
PTY fixture, hover/drag scrollbar behavior, theme switching, the per-pane color
override, alternate-screen rendering, and returning to the shell. It also
replays conversation paint captured from Claude Code 2.1.307 and Codex 0.160.0
resuming generated local transcripts. The captures and tests make no model
requests. Run with `--small` to check the minimum window size, and run
`node --test native/tests/conversation_unit.cjs` for message classification and
ANSI-color preservation. The color adapter targets the locked xterm DOM
renderer; repeat these checks when updating xterm or supported CLI layouts.
