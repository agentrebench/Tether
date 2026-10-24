# Native terminal Python boundary

The macOS host keeps the existing Python agent engine, tools, knowledge model,
sessions, and shell-theme support. Its private `tether native-app` command has
four actions:

- `settings`: one JSON line containing protocol `1`, `shell`, `theme`, `font`,
  `theme_file`, `home`, and `themes` (`id`, `name`, `source`). No credentials or
  unrelated config is serialized. Output is limited to 1 MiB.
- `save-settings`: read one JSON line of at most 16 KiB, containing exactly the
  four string fields `shell`, `theme`, `font`, and `theme_file`. Reject duplicate
  or unknown keys and invalid values, save through the existing appearance
  helper, then return the settings object. It does not wait for input EOF.
- `shell [--theme ID] [--cwd PATH]`: claim the supplied PTY, run a login shell
  in its own foreground process group, preserve existing startup files, and own
  the private theme directory until the shell exits. The theme override affects
  only that pane and never saves preferences. The host supplies bundled commands
  through PATH. HUP/TERM close the shell and its jobs; normal terminal Ctrl+C
  targets the foreground job. The native host must keep draining PTY output.
- `agent [--cwd PATH]`: claim the supplied PTY and enter the existing agent
  chooser followed by compact chat. Model selection, approvals, clarifying
  questions, cancellation, queueing, and persistence keep their existing behavior.

Settings failures return `{ "protocol": 1, "error": "..." }` with exit code 2.
Shell and agent failures use their terminal's ordinary error display.

## Activity OSC

The agent action enables `TETHER_NATIVE_ACTIVITY=1`. Only an opted-in TTY emits:

```
ESC ] 777 ; tether ; 1 ; SEQUENCE ; PHASE BEL
```

The decimal sequence increases for each changed phase in one emitter/PTY.
Valid phases are `thinking`, `reasoning`, `working`, `waiting`, and `idle`.
`thinking` starts at an actual model request; `reasoning` starts when the provider
reports its private reasoning channel. Text/tool activity clears the orb.
`waiting` covers approvals and questions, and `idle` covers completion,
cancellation, errors, and cleanup. Subagent snapshots expose an additive enum
`phase` so concurrent model work can contribute without exposing model content.
The messages never contain reasoning text, prompts, tool arguments, or secrets.
They are UI metadata, never an approval, authentication, or execution channel.

The host consumes OSC 777 only for its agent pane, validates the small enum and
sequence, and discards activity on PTY exit. The interactive light may be visible
only for `thinking` or `reasoning`. Waiting for a user's answer must take priority.
Existing terminals, plain pipes, and the separate NDJSON desktop bridge remain
usable without this extension.

## Compact composer resize

The opted-in compact composer also emits bounded, content-free OSC 777 messages:

```
ESC ] 777 ; tether ; 1 ; input ; ready|prepared|closed BEL
```

`ready` advertises the active prompt's resize binding; `closed` withdraws it.
Before changing this pane's terminal grid, the display sends `ESC [ 24 ; 2 ~`
(F24 / Shift-F12). The prompt temporarily suspends rendering through
prompt_toolkit's `in_terminal`, erases the composer at the old grid size, and
emits `prepared`. The display then resizes xterm and the PTY together. Once the
PTY size changes, the prompt redraws at the new size and advertises `ready`.
This prevents normal-buffer reflow from shifting the live prompt before its
old contents can be erased. Transcript lines and the input buffer are retained.

At most one preparation is pending per pane. The Python wait is bounded to
500 ms; the display falls back after 750 ms if no acknowledgement arrives.
Raw input remains queued without echo during preparation; cancellation unwinds
the prompt_toolkit context. Closing the pane cancels its display timer.
No binding or OSC is installed for non-native/non-compact prompts. The existing
activity sequence is independent of these composer messages.
