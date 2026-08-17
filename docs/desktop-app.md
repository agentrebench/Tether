# Tether Desktop

Tether Desktop is a React and TypeScript developer console packaged with
Tauri 2 for macOS and Linux. The Python agent engine remains the single
implementation of model providers, tools, permissions, memory, and agent
behavior.

## Architecture

```text
React + TypeScript
       |
       | typed Tauri commands and events
       v
Tauri Rust host
       |
       | supervised stdin/stdout process
       v
tether app-bridge (NDJSON)
       |
       v
QueryEngine -> tools -> model/provider
```

The browser layer has no general shell capability. Four Rust commands form its
native boundary:

- `start_bridge` validates the selected project, locates the installed
  `tether` executable, and launches the project-scoped bridge;
- `send_bridge` writes one structured protocol message;
- `stop_bridge` gracefully stops the child and kills it if it does not exit;
- `read_workspace_entry` resolves workspace-confined file and directory
  previews, including shortened paths used by assistant file citations.

Every bridge launch has a unique ID. React ignores late events from an older
process after the user changes projects. API-key values never cross the bridge;
the UI receives only whether credentials are configured.

The current desktop event contract is protocol version 3. It adds queued-turn,
approval-resolution, direction-request, and numbered-agent snapshot events.
The desktop rejects an older installed CLI with an explicit update message
instead of entering a partially working state.

## Current vertical slice

- a first-run quick-start guide that explains the project-scoped agent loop,
  provider-independent mental model, tool surface, and safety boundaries, with
  a permanent sidebar entry point for reopening it;
- persistent light and dark themes plus a dedicated native window drag region;
- native folder picker with remembered workspace;
- server-backed Memory and Plan mode switches directly below Runtime; desktop
  memory is off by default, while provider/model switches retain the live chat;
- multi-turn Markdown and GitHub-flavored Markdown chat;
- a composer that remains writable during generation and queues follow-up
  prompts in FIFO order without merging their turn identities;
- provider and model selectors for local GGUF, DeepSeek, Kimi, OpenAI, GLM,
  Anthropic, Codex, and custom OpenAI-compatible endpoints;
- write-only, per-provider API-key setup (environment variables still take
  precedence) and reasoning/thinking controls where the API supports them;
- local GGUF directory discovery and managed `llama-server` startup;
- live response text, private-reasoning activity, and tool lifecycle updates;
- line-level SSE consumption plus frame-batched rendered Markdown so the first
  response delta is not held behind transport or per-token repaint work;
- clickable assistant path mentions with shortened-path resolution and a
  workspace-confined, right-side code preview with line numbers, syntax
  highlighting, and theme-aware colors, including Racket, Scheme, Common Lisp,
  Clojure, Emacs Lisp, Fennel, and Hy source files;
- structured, expandable tool cards with argument/result previews, stable
  error codes, concise tool descriptions, purple running states, and one
  per-tool row with a multiplier for repeated calls;
- numbered, independently expandable sub-agent cards with each agent's task,
  safe nested-tool activity, status, elapsed time, usage, and final result;
- a backend-cataloged, clickable and type-ahead slash-command palette matching
  the terminal's current command registry, including the persistent codebase
  mental model, skills and bundles, memory, sessions, runtime controls, and
  installed `/skill-name` commands;
- streaming auto-follow that pauses as soon as the user scrolls away from the
  latest output and resumes only when they return or choose Follow output;
- native clarification cards for the existing `ask_user` tool;
- a project todo checklist that persists across launches only when Memory is
  enabled and is cleared explicitly by New session;
- background shell execution with list, output, and stop controls;
- optional LSP definitions, references, hover, and document symbols;
- cancellation and clean session reset, including queued-follow-up cleanup;
- Allow once, Allow full session, and No controls for restricted tools. No
  terminates the active turn without running later calls from that model batch
  and asks the user what Tether should do instead;
- post-turn tool and token summaries;
- project-scoped process lifecycle and stale-process protection;
- canonical path enforcement for all file/search/LSP tools, explicit outside-user
  path rejection for shell commands, and an OS-level write sandbox;
- native macOS and Linux packages.

## Development

All platforms require Node.js 20 or newer, current stable Rust and Cargo, and an
editable Tether CLI installation (normally through pipx).

macOS additionally requires Xcode Command Line Tools. Linux requires Tauri's
WebKitGTK build dependencies. On Ubuntu 22.04 or another compatible Debian-based
system:

```bash
sudo apt-get update
sudo apt-get install -y \
  libwebkit2gtk-4.1-dev \
  libappindicator3-dev \
  librsvg2-dev \
  bubblewrap \
  patchelf \
  rpm
```

Install and run:

```bash
cd desktop
npm install
npm run desktop:dev
```

Build the production web bundle only:

```bash
npm run build
```

## Native packages

Build the macOS app and DMG on macOS:

```bash
cd desktop
npm run dmg
```

The versioned installer is copied to `dist/macos/`.

> **Linux is experimental.** The Rust host, frontend, and packages build in CI
> (`.github/workflows/desktop-linux.yml`), and the code paths are POSIX/`bwrap`
> aware, but the app has not been run end to end on a Linux desktop yet. Known
> likely gaps: `titleBarStyle: Overlay` / `hiddenTitle` are macOS-only (a
> normal GTK title bar appears and the top drag strip may leave dead space),
> WebKitGTK may render some CSS differently, and Wayland/X11 file-dialog and
> drag-region behavior is unverified. Reports welcome.

Build AppImage, Debian, and RPM packages on Linux:

```bash
cd desktop
npm run linux
```

The packages and `SHA256SUMS` are copied to `dist/linux/`. Native output also
remains under `desktop/src-tauri/target/release/bundle/`. Linux packages must be
built on Linux; the repository workflow `.github/workflows/desktop-linux.yml`
does this on Ubuntu 22.04 and uploads the resulting artifacts.

For broad AppImage compatibility, build on the oldest Linux baseline you intend
to support. A package built against a newer glibc cannot be assumed to run on an
older distribution.

## Engine setup (the app installs it)

The desktop app runs the Python engine through the `tether` CLI. On launch it
checks the machine (`check_environment` in the Rust host: Python 3.10+, pipx,
the CLI, llama-server, git/cmake, Xcode CLT on macOS, bubblewrap on Linux) and,
if the CLI is missing, opens the **Set up Tether** dialog instead of failing.
One click runs the install in the Rust host and streams the log into the
dialog; when it finishes the app connects to your project by itself. Nothing
needs a terminal or sudo:

- **Install Tether CLI** — installs pipx (Homebrew if present, else
  `python -m pip install --user pipx`), then `pipx install git+<repo>`. If a
  pipx install of `tether` already exists (including a developer's editable
  checkout) it is upgraded in place, never replaced.
- **Set up local models** — installs cmake if needed (Homebrew) and runs
  `tether setup`, which clones and builds llama.cpp into `~/.tether/llama.cpp`
  (or beside a git checkout of Tether). Offered from the runtime sheet's Local
  provider when llama-server is not built.

The wrench button in the sidebar reopens the dialog any time to re-check,
update the CLI, or build llama.cpp. `tether doctor` (`--json`) prints the same
facts from the terminal.

The Rust host finds the CLI via `TETHER_CLI`, the process `PATH`, pipx's
`~/.local/bin/tether`, standard system paths, macOS Python user-bin
directories, and Homebrew locations. Desktop apps do not reliably inherit shell
startup files. Add provider keys in the runtime sheet, use `tether key set`, or
expose them through the app's launch environment.

API keys are stored only in the user config at `~/.tether/config.json`, which
Tether writes with mode `0600`. They are never bundled into the web app or
native package. Environment variables take precedence over stored keys.

### Signing and notarizing the macOS build

`npm run dmg` (`scripts/package_macos.sh`) signs and notarizes automatically
when the machine has the material; without it, it falls back to the previous
ad-hoc, local-testing-only build and says so.

One-time setup on the release Mac:

1. Install a **Developer ID Application** certificate in the login keychain
   (Xcode → Settings → Accounts → Manage Certificates, or download it from
   developer.apple.com and double-click). Check with
   `security find-identity -v -p codesigning`.
2. Store notarization credentials in the keychain (never in the repo or env):

   ```bash
   xcrun notarytool store-credentials tether-notary \
     --apple-id you@example.com --team-id TEAMID --password <app-specific-password>
   # or, with an App Store Connect API key:
   xcrun notarytool store-credentials tether-notary \
     --key ~/.private_keys/AuthKey_KEYID.p8 --key-id KEYID --issuer ISSUER-UUID
   ```

Then `npm run dmg`. The script auto-detects the first Developer ID identity
(override with `APPLE_SIGNING_IDENTITY`), signs the app with the hardened
runtime and `src-tauri/entitlements.plist`, notarizes and staples the `.app`,
builds the DMG, signs it, notarizes and staples the DMG, and finishes with a
`spctl` Gatekeeper assessment. `TETHER_NOTARY_PROFILE` selects a different
keychain profile; `TETHER_SKIP_NOTARIZE=1` signs without notarizing.

Linux packages do not require Apple signing, but release packages should
still be checksumed and published from a controlled CI build.

## Workspace and tool security

The desktop bridge creates one enforced workspace policy and shares it with
the main agent and any sub-agents. File reads, writes, edits, globs, greps, and
LSP requests canonicalize relative and absolute paths and reject symlink
escapes. A denied result has the stable `WORKSPACE_PATH_DENIED` error code.

Shell commands need normal access to system compilers, package managers, and
the network, so Tether combines an explicit user-path guard with an OS write
sandbox rather than claiming complete read isolation. The guard rejects home
expansion, parent traversal, and absolute user-data paths outside the selected
project or temporary storage before launch. On macOS, Seatbelt allows writes
only below the selected project and system temporary directories. On Linux,
bubblewrap mounts the host read-only and binds only the project and temporary
directories as writable. If the platform sandbox is missing, shell execution
returns `SANDBOX_UNAVAILABLE`; it never silently falls back to an unrestricted
shell.

When Memory is enabled, task checklists are stored under
`~/.tether/app_state/` using a hash of the canonical project path and owner-only
file permissions. With Memory off, a launch begins with no restored checklist.
Background jobs are owned by the bridge process and terminated when the bridge or session closes.
Job output is bounded to prevent an unbounded GUI or model-context buffer.

The LSP tool launches an installed language server only when invoked. Supported
adapters are TypeScript/JavaScript (`typescript-language-server`), Python
(`pyright-langserver` or `pylsp`), Rust (`rust-analyzer`), and Go (`gopls`). A
missing server produces `LSP_UNAVAILABLE` and does not affect ordinary tools.

## Verification

```bash
cd desktop
npm run build
cargo fmt --check --manifest-path src-tauri/Cargo.toml
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets --all-features -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
```

The Python bridge and engine remain covered by the repository's unit suite.

## Next milestones

1. Add saved and resumable desktop conversations (the task checklist is already
   durable per project).
2. Add full diff review and file citations beyond the current structured
   preview cards.
3. Bundle a managed Python runtime and Tether wheel as a sidecar so the app
   needs no Python on the machine at all (today it installs the CLI for you but
   still needs Python 3.10+ or Homebrew present).
4. Move signing/notarization into CI (the local `npm run dmg` pipeline is signed and notarized when the build Mac has the material; Linux release packages should be built and checksumed from a controlled CI run).
