# Terminal startup and zsh themes

Bare interactive `tether` opens a real login shell in a terminal workspace. It
performs no provider authentication or model startup. `tether terminal` explicitly
opens it; `tether chat`, `start`, and `run` retain the full agent workspace.
`--plain`, explicit resumes/models, pipes, and `TERM=dumb` retain direct chat.
The optional Agent button starts Tether's own compact chat in a separate PTY tab.

Click Settings to search the 142 included Oh My Zsh themes and any installed
themes. The catalog reads filenames
only; searching never sources theme code. It discovers `$ZSH`, `~/.oh-my-zsh`,
common system locations, and `$ZSH_CUSTOM`, including one nested custom-theme
directory. Custom themes override bundled names as they do in Oh My Zsh. The
catalog is capped at 2,000 Oh My Zsh entries. Installed and custom versions take
precedence over the included copy of the same theme. The included collection,
built-in prompts, and custom-file option work without a framework installation
or network access. The native macOS app uses this same catalog.

Preview opens the selected theme in a real shell without persisting changes.
Only one unsaved preview is retained; trying another closes the earlier preview.
Use theme saves and keeps that shell. Save opens a new themed shell, preserving
existing terminals and jobs. There are at most eight terminal processes. Closing
a tab terminates its shells and foreground jobs; exiting the last shell exits
Tether. On small windows, hidden tabs/panes stay alive. New shells start in the
launch directory. F2/F4/F6/F7/F8/F9 control settings/new/pane/tab/split/agent;
Ctrl+C/D/W/Z and ordinary input remain shell keys. Mouse-wheel up or
Shift+PageUp opens the terminal backend's bounded scrollback; Esc returns to live
output. Applications that enable terminal mouse reporting receive their wheel
events directly.

CLI alternatives:

```sh
tether theme list
tether theme show
tether theme set agnoster
tether theme set omz:robbyrussell
tether theme set powerline --shell zsh --font nerd
tether theme set custom --file ~/.config/zsh/my-theme.zsh
tether theme set inherit
```

| Setting | Default | Values |
| --- | --- | --- |
| `terminal_shell` | empty | executable path/name; automatic uses `$SHELL`, or zsh for a selected theme |
| `terminal_theme` | `inherit` | `inherit`, `minimal`, `powerline`, `custom`, `omz:<theme name>` |
| `terminal_font_mode` | `ascii` | `ascii`, `powerline`, `nerd` |
| `terminal_theme_file` | empty | local zsh code, used only by `custom` |

Theme mode creates a private startup directory for each shell, forwards
`.zshenv`, `.zprofile`, `.zshrc`, and `.zlogin` in order, then loads the chosen
theme. Startup files see their normal `ZDOTDIR`, including redirects to another
configuration directory. After startup the original `ZDOTDIR` is restored; nested
shells and logout use their normal configuration. Temporary files are cleaned
when the process exits or is cancelled. No shell startup files are rewritten.
Startup configurations that disable subsequent startup files may disable themes.

Oh My Zsh themes reuse existing framework helpers when startup has loaded them.
For an included theme on a fresh shell, Tether loads only the bundled upstream
prompt helpers (Git, VCS, language-version placeholders, and colors). It does
not initialize a framework, update checks, aliases, history, or completion
settings. For a theme from a local Oh My Zsh installation, Tether retains its
existing framework initialization with private caches, no plugins, and updates
disabled. Static selected prompts are restored
after older prompt hooks; themes with their own hooks retain their behavior.
Advanced themes can still require their own dependencies or user configuration.
A theme is executable local zsh code and runs only when its shell is opened.
Filenames are quoted data, never interpolated into shell commands.

Built-in prompts use a final `precmd` hook and zsh prompt escapes, with no Git
commands or network operations. Powerline emits U+E0B0 separators; Nerd adds a
U+F07B folder icon; ASCII uses ordinary characters. These modes affect Tether's
built-in Powerline prompt, not third-party themes. The host terminal selects and
renders the font; Tether does not install fonts or change that application's
font settings. The native macOS app already bundles Tether Mono with Nerd Font
and Powerline glyphs; choose that font in its Settings for themes such as agnoster.

## Included upstream source

`vendor/oh-my-zsh/` contains 142 unmodified `.zsh-theme` files, seven prompt-helper
libraries, the upstream MIT license, and a provenance/hash manifest. The source
is [Oh My Zsh commit 2100c630ba4bd67bb48802c3e7da893a4fdaa1a0](https://github.com/ohmyzsh/ohmyzsh/tree/2100c630ba4bd67bb48802c3e7da893a4fdaa1a0).
`manifest.json` records the upstream archive SHA-256 and every included file's
hash. Install/update scripts and plugins are not bundled. Python wheels and the
frozen CLI include this pack; the native app gets it through the bundled engine.
Theme-specific optional tools still need to be present to show their data.
The `random` theme retains upstream behavior and can select custom themes too.

Verification covers real PTY command execution, interrupt, editing shortcuts,
splits, optional agent selection, theme preview/save/cancel, framework discovery,
startup redirects, filename quoting, cleanup, and small-window mouse interaction.
`tests/test_bundled_themes.py` checks the upstream hashes and syntax of all 142
themes, representative fresh-shell startup, local override precedence, and real
PTY preview/save at 60 and 110 columns. Run the native fixture harness with
`--themes` for theme search, agnoster rendering, persistence, and shell preservation;
add `--engine dist/tether` to test the packaged engine, and `--small` for a 680×440
window. See [the native app tests](native-macos-app.md).
