# Native terminal process core

`core/tether_runtime.c` owns PTYs and child process lifetimes. The existing Python
agent and theme logic remain separate. This is a macOS C23 module, tested with
Apple Clang 21; it deliberately needs no new event-loop dependency because AppKit
already supplies the owner thread and timer. All API calls belong to that thread.

## Host integration

1. Before AppKit or other threads start, recognize `--pty-child` and return
   `tt_runtime_child_main(argc, argv)`. Pass the current executable's absolute
   path to `tt_runtime_create` in the regular application process.
2. Supply absolute executable paths, literal argument arrays, the full desired
   environment (or NULL to inherit), and an absolute working directory. The
   runtime never inserts a shell or interpolates command strings.
3. Set `pty=true` for shell, agent, or btop terminals. Set `defer_tty=true` only
   for the frozen Python executable whose Python child will call setsid and
   TIOCSCTTY. The C helper otherwise claims the controlling terminal itself.
   For bounded configuration jobs, `pty=false` uses separate stdin/output pipes;
   stderr and stdout are combined. The host must validate configuration framing
   and treat errors/malformed output as failure.
4. Pump on the existing UI timer. One pump reads at most 64 KiB total; each
   session stops reading at 256 KiB of unacknowledged data. Acknowledge only
   after the terminal renderer consumes the output. Without an output callback,
   output is deliberately discarded (used during shutdown).
5. Writes enqueue all bytes or reject the whole write when the 64 KiB input
   queue cannot accept them. The host should split or retry large pastes. There
   are at most 16 sessions, and session IDs never repeat during runtime lifetime.
6. Close requests are idempotent. The runtime sends HUP once to observed
   foreground/session groups and retries discovery if a child is not ready.
   It keeps a frozen bootloader alive so it can reap Python and clean extraction
   files. After 0.5 seconds (ordinary) or 2 seconds (deferred), it escalates to
   SIGKILL. It reaps the direct child with nonblocking waitpid. Exit callbacks
   fire once after available output has drained, and release the slot first.

The destructor runs bounded shutdown for at most about three seconds and closes
all owned descriptors. As with any bounded userspace supervisor, an OS process
stuck in uninterruptible kernel I/O can outlive the deadline; this module does
not promise to terminate such a process or daemons deliberately detached into
unrelated sessions. Ordinary foreground jobs, shell groups, and the simulated
frozen-loader lifecycle are covered by real process tests. The owner must not
install SIGCHLD=SIG_IGN, reap the runtime's children, or destroy/pump the runtime
recursively from a callback.

`tt_runtime_foreground_pid` reads the foreground process group of a live PTY,
returning -1 for pipes, missing panes, or closing panes. The macOS host checks
shell panes twice a second and sends only a recognized `claude`/`codex` enum to
the display, or an empty string for other processes. It does not read process
arguments, environment, or model messages. This metadata enables optional
conversation coloring without changing the PTY output or input.

`core/tether_files.c` is a separate, stateless local file reader used by the
Explorer. The caller owns a heap-allocated `tt_file_result` and may invoke it on
a worker queue. It walks relative paths from an open root directory, rejects
parent traversal and symlink components, checks regular-file types, and uses
nonblocking opens to avoid hanging on FIFOs. Listings and reads have fixed
limits declared in `tether_files.h`. It owns and closes all temporary descriptors;
it never executes file content or invokes the Python engine. Tests cover bounds,
sorting, hidden/binary/empty files, removed paths, and directory replacement by
a symlink between reads.

## Build and verification

```sh
cmake -S native -B build/native -DCMAKE_BUILD_TYPE=Debug -DTETHER_SANITIZERS=ON
cmake --build build/native
ctest --test-dir build/native --output-on-failure
```

The host can link the `tether_runtime` static library. CMake uses C23 and strict
warnings (`-Wall -Wextra -Werror -Wpedantic`). ASan and UBSan are optional together.
Ninja is suitable when installed; the tested local environment used the CMake
Unix Makefiles generator because Ninja was not installed.

Nine test scenarios execute real children: zsh command I/O and resize, Ctrl+C,
literal arguments and paths with spaces, inherited descriptor closure, pipe
input, output credit/backpressure, input/session caps, close-before-ready and
duplicate close, forced kill/reaping, frozen-loader cleanup, exact environment
passing, and setup/exec failures. No provider, network access, or credentials
are used. This validates behavior; it is not a migration speedup benchmark.
