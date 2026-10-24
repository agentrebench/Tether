#ifndef TETHER_RUNTIME_H
#define TETHER_RUNTIME_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

/* All calls and callbacks run on one owner thread. Callbacks may enqueue input,
 * acknowledge output or request closure, but must not pump/destroy the runtime. */
#define TT_MAX_SESSIONS 16U
#define TT_INPUT_LIMIT (64U * 1024U)
#define TT_FRAME_OUTPUT_LIMIT (64U * 1024U)
#define TT_OUTPUT_CREDIT (256U * 1024U)

typedef uint64_t tt_session_id;
typedef struct tt_runtime tt_runtime;

typedef struct {
    const char *const *argv; /* NULL-terminated; argv[0] must be absolute. */
    const char *const *envp; /* NULL inherits the host environment. */
    const char *cwd;        /* Absolute directory; NULL inherits host cwd. */
    unsigned cols;
    unsigned rows;
    bool pty;
    bool defer_tty;         /* PyInstaller Python child claims the tty. */
} tt_launch;

typedef void (*tt_output_fn)(void *context, tt_session_id session,
                             const unsigned char *bytes, size_t count);
typedef void (*tt_exit_fn)(void *context, tt_session_id session, int exit_code);

tt_runtime *tt_runtime_create(const char *absolute_helper);
void tt_runtime_destroy(tt_runtime *runtime);
tt_session_id tt_runtime_spawn(tt_runtime *runtime, const tt_launch *launch,
                               char *error, size_t error_capacity);
/* Accepted bytes are owned by the runtime. False leaves the input queue
 * unchanged (closed/unknown session, invalid input, or insufficient capacity). */
bool tt_runtime_write(tt_runtime *runtime, tt_session_id session,
                      const void *bytes, size_t count);
bool tt_runtime_resize(tt_runtime *runtime, tt_session_id session,
                       unsigned cols, unsigned rows);
/* Acknowledge only bytes consumed by the terminal renderer. Extra credit is
 * clamped; a newly spawned session starts with TT_OUTPUT_CREDIT bytes. */
void tt_runtime_ack(tt_runtime *runtime, tt_session_id session, size_t count);
void tt_runtime_request_close(tt_runtime *runtime, tt_session_id session);
/* Nonblocking. Output pointers remain valid only for the callback. Exit fires
 * once, after output drains, and releases the slot before invoking the callback.
 * The host supplies its own timer/event loop; pump does not create one. */
void tt_runtime_pump(tt_runtime *runtime, tt_output_fn output, tt_exit_fn exited,
                     void *context);
size_t tt_runtime_live_count(const tt_runtime *runtime);
pid_t tt_runtime_child_pid(const tt_runtime *runtime, tt_session_id session);
/* Foreground process group of a live PTY; -1 for pipes/unknown/closed panes. */
pid_t tt_runtime_foreground_pid(const tt_runtime *runtime, tt_session_id session);

/* Invoke before initializing AppKit/threads when argv[1] is --pty-child.
 * Private argv format is validated; exits 125 on setup failure, 127 on exec
 * failure. No command strings are interpreted and no shell is inserted. */
int tt_runtime_child_main(int argc, char *argv[]);

#endif
