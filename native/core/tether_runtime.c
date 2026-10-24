#include "tether_runtime.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#ifdef __APPLE__
#include <util.h>
#else
#include <pty.h>
#endif

extern char **environ;

enum { TT_ARG_LIMIT = 256, TT_READ_CHUNK = 16384 };
static const double CLOSE_GRACE = 0.50;
static const double FROZEN_CLOSE_GRACE = 2.0;

typedef struct {
    tt_session_id id;
    pid_t pid;
    int input_fd;
    int output_fd;
    bool pty;
    bool defer_tty;
    bool closing;
    bool forced;
    bool reaped;
    bool eof;
    int exit_code;
    double close_started;
    pid_t foreground;
    pid_t tty_session;
    pid_t hungup_foreground;
    pid_t hungup_session;
    bool hungup_child;
    size_t pending;
    size_t outstanding;
    unsigned char input[TT_INPUT_LIMIT];
} tt_session;

struct tt_runtime {
    char *helper;
    tt_session_id next_id;
    unsigned next_slot;
    tt_session sessions[TT_MAX_SESSIONS];
};

static double monotonic_now(void) {
    struct timespec value;
    (void)clock_gettime(CLOCK_MONOTONIC, &value);
    return (double)value.tv_sec + (double)value.tv_nsec / 1000000000.0;
}

static void set_error(char *error, size_t capacity, const char *context,
                      int code) {
    if (error != NULL && capacity > 0)
        (void)snprintf(error, capacity, "%s: %s", context, strerror(code));
    errno = code;
}

static bool descriptor_flags(int fd, bool nonblocking) {
    int flags = fcntl(fd, F_GETFD);
    if (flags < 0 || fcntl(fd, F_SETFD, flags | FD_CLOEXEC) < 0) return false;
    if (nonblocking) {
        flags = fcntl(fd, F_GETFL);
        if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0) return false;
    }
#ifdef F_SETNOSIGPIPE
    if (fcntl(fd, F_SETNOSIGPIPE, 1) < 0) return false;
#endif
    return true;
}

static void close_io(tt_session *session) {
    if (session->input_fd >= 0 && session->input_fd != session->output_fd)
        (void)close(session->input_fd);
    if (session->output_fd >= 0) (void)close(session->output_fd);
    session->input_fd = -1;
    session->output_fd = -1;
    session->pending = 0;
    session->eof = true;
}

static tt_session *find_session(tt_runtime *runtime, tt_session_id id) {
    if (runtime == NULL || id == 0) return NULL;
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i)
        if (runtime->sessions[i].id == id) return &runtime->sessions[i];
    return NULL;
}

tt_runtime *tt_runtime_create(const char *absolute_helper) {
    if (absolute_helper == NULL || absolute_helper[0] != '/' ||
        access(absolute_helper, X_OK) != 0) {
        errno = EINVAL;
        return NULL;
    }
    tt_runtime *runtime = calloc(1, sizeof(*runtime));
    if (runtime == NULL) return NULL;
    runtime->helper = strdup(absolute_helper);
    if (runtime->helper == NULL) { free(runtime); return NULL; }
    runtime->next_id = 1;
    return runtime;
}

static bool launch_valid(const tt_launch *launch, size_t *argc) {
    if (launch == NULL || launch->argv == NULL || launch->argv[0] == NULL ||
        launch->argv[0][0] != '/' ||
        (launch->cwd != NULL && launch->cwd[0] != '/') ||
        (launch->pty && (launch->cols == 0 || launch->rows == 0 ||
                         launch->cols > USHRT_MAX || launch->rows > USHRT_MAX))) {
        errno = EINVAL;
        return false;
    }
    for (*argc = 0; *argc < TT_ARG_LIMIT; ++*argc)
        if (launch->argv[*argc] == NULL) break;
    if (*argc == TT_ARG_LIMIT) { errno = E2BIG; return false; }
    if (access(launch->argv[0], X_OK) != 0) return false;
    if (launch->cwd != NULL) {
        struct stat info;
        if (stat(launch->cwd, &info) != 0) return false;
        if (!S_ISDIR(info.st_mode)) { errno = ENOTDIR; return false; }
    }
    return true;
}

tt_session_id tt_runtime_spawn(tt_runtime *runtime, const tt_launch *launch,
                               char *error, size_t error_capacity) {
    size_t argc = 0;
    if (error != NULL && error_capacity > 0) error[0] = '\0';
    if (runtime == NULL || !launch_valid(launch, &argc)) {
        set_error(error, error_capacity, "Invalid terminal launch",
                  runtime == NULL ? EINVAL : errno);
        return 0;
    }
    tt_session *session = NULL;
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i)
        if (runtime->sessions[i].id == 0) { session = &runtime->sessions[i]; break; }
    if (session == NULL || runtime->next_id == 0) {
        set_error(error, error_capacity, "Terminal session limit",
                  session == NULL ? EUSERS : EOVERFLOW);
        return 0;
    }

    /* Descriptor slots: parent input, parent output, child input, child output.
     * A PTY shares the parent and child sides; pipes keep them separate. */
    int fd[4] = {-1, -1, -1, -1};
    int result = 0;
    if (launch->pty) {
        struct winsize size = {.ws_row = (unsigned short)launch->rows,
                               .ws_col = (unsigned short)launch->cols};
        if (openpty(&fd[0], &fd[2], NULL, NULL, &size) != 0) result = errno;
        fd[1] = fd[0]; fd[3] = fd[2];
    } else {
        int in[2] = {-1, -1}, out[2] = {-1, -1};
        if (pipe(in) != 0) result = errno;
        else if (pipe(out) != 0) { result = errno; close(in[0]); close(in[1]); }
        else { fd[0] = in[1]; fd[1] = out[0]; fd[2] = in[0]; fd[3] = out[1]; }
    }
    for (unsigned i = 0; result == 0 && i < 4; ++i)
        if (!descriptor_flags(fd[i], i < 2)) result = errno;

    char *child_argv[TT_ARG_LIMIT + 6];
    child_argv[0] = runtime->helper;
    child_argv[1] = (char *)"--pty-child";
    child_argv[2] = (char *)(!launch->pty ? "--pipe" :
                            launch->defer_tty ? "--defer-tty" : "--claim-tty");
    child_argv[3] = (char *)(launch->cwd != NULL ? launch->cwd : "");
    child_argv[4] = (char *)"--";
    for (size_t i = 0; i <= argc; ++i) child_argv[i + 5] = (char *)launch->argv[i];

    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attributes;
    bool actions_ready = false, attributes_ready = false;
    if (result == 0) { result = posix_spawn_file_actions_init(&actions); actions_ready = result == 0; }
    if (result == 0) { result = posix_spawnattr_init(&attributes); attributes_ready = result == 0; }
    if (result == 0) result = posix_spawn_file_actions_adddup2(&actions, fd[2], STDIN_FILENO);
    if (result == 0) result = posix_spawn_file_actions_adddup2(&actions, fd[3], STDOUT_FILENO);
    if (result == 0) result = posix_spawn_file_actions_adddup2(&actions, fd[3], STDERR_FILENO);
    for (unsigned i = 0; result == 0 && i < 4; ++i) {
        bool duplicate = false;
        for (unsigned j = 0; j < i; ++j) if (fd[j] == fd[i]) duplicate = true;
        if (!duplicate && fd[i] > STDERR_FILENO)
            result = posix_spawn_file_actions_addclose(&actions, fd[i]);
    }
    sigset_t empty, defaults;
    sigemptyset(&empty);
    sigemptyset(&defaults);
    const int signals[] = {SIGINT, SIGQUIT, SIGHUP, SIGTERM, SIGPIPE, SIGCHLD,
                           SIGTSTP, SIGTTIN, SIGTTOU};
    for (size_t i = 0; i < sizeof(signals) / sizeof(signals[0]); ++i)
        sigaddset(&defaults, signals[i]);
    if (result == 0) result = posix_spawnattr_setsigmask(&attributes, &empty);
    if (result == 0) result = posix_spawnattr_setsigdefault(&attributes, &defaults);
    short flags = POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF;
#ifdef POSIX_SPAWN_CLOEXEC_DEFAULT
    flags |= POSIX_SPAWN_CLOEXEC_DEFAULT;
#endif
    if (result == 0) result = posix_spawnattr_setflags(&attributes, flags);
    pid_t child = -1;
    if (result == 0)
        result = posix_spawn(&child, runtime->helper, &actions, &attributes,
                             child_argv, launch->envp != NULL ?
                             (char *const *)launch->envp : environ);
    if (actions_ready) posix_spawn_file_actions_destroy(&actions);
    if (attributes_ready) posix_spawnattr_destroy(&attributes);
    for (unsigned i = result == 0 ? 2 : 0; i < 4; ++i) {
        bool duplicate = false;
        for (unsigned j = result == 0 ? 2 : 0; j < i; ++j)
            if (fd[j] == fd[i]) duplicate = true;
        if (!duplicate && fd[i] >= 0) close(fd[i]);
    }
    if (result != 0) { set_error(error, error_capacity, "Cannot launch terminal", result); return 0; }
    memset(session, 0, sizeof(*session));
    session->id = runtime->next_id++;
    session->pid = child;
    session->input_fd = fd[0]; session->output_fd = fd[1];
    session->pty = launch->pty; session->defer_tty = launch->defer_tty;
    return session->id;
}

bool tt_runtime_write(tt_runtime *runtime, tt_session_id id,
                      const void *bytes, size_t count) {
    tt_session *session = find_session(runtime, id);
    if (session == NULL || session->closing || session->reaped ||
        session->input_fd < 0 || count > TT_INPUT_LIMIT - session->pending ||
        (count > 0 && bytes == NULL)) return false;
    if (count > 0) memcpy(session->input + session->pending, bytes, count);
    session->pending += count;
    return true;
}

bool tt_runtime_resize(tt_runtime *runtime, tt_session_id id,
                       unsigned cols, unsigned rows) {
    tt_session *session = find_session(runtime, id);
    if (session == NULL || !session->pty || session->output_fd < 0 ||
        cols == 0 || rows == 0 || cols > USHRT_MAX || rows > USHRT_MAX) return false;
    struct winsize size = {.ws_row = (unsigned short)rows, .ws_col = (unsigned short)cols};
    return ioctl(session->output_fd, TIOCSWINSZ, &size) == 0;
}

void tt_runtime_ack(tt_runtime *runtime, tt_session_id id, size_t count) {
    tt_session *session = find_session(runtime, id);
    if (session != NULL)
        session->outstanding -= count < session->outstanding ? count : session->outstanding;
}

static bool safe_signal_group(pid_t group, int signal_number) {
    return group > 1 && group != getpgrp() && kill(-group, signal_number) == 0;
}

static void capture_terminal_groups(tt_session *session) {
    if (!session->pty || session->output_fd < 0) return;
    pid_t foreground = tcgetpgrp(session->output_fd);
    if (foreground > 1 && foreground != getpgrp()) {
        session->foreground = foreground;
        pid_t leader = getsid(foreground);
        if (leader > 1 && leader != getsid(0)) session->tty_session = leader;
    }
}

static void signal_session(tt_session *session, int signal_number) {
    capture_terminal_groups(session);
    if (signal_number != SIGHUP || session->foreground != session->hungup_foreground) {
        if (safe_signal_group(session->foreground, signal_number) && signal_number == SIGHUP)
            session->hungup_foreground = session->foreground;
    }
    if (session->tty_session != session->foreground &&
        (signal_number != SIGHUP || session->tty_session != session->hungup_session)) {
        if (safe_signal_group(session->tty_session, signal_number) && signal_number == SIGHUP)
            session->hungup_session = session->tty_session;
    }
    /* Let a frozen bootloader reap Python and remove its extraction directory.
     * Before Python claims the tty, continue retrying through the grace period. */
    if (!session->defer_tty || signal_number == SIGKILL) {
        if (session->pid != session->foreground && session->pid != session->tty_session &&
            (signal_number != SIGHUP || !session->hungup_child)) {
            if (safe_signal_group(session->pid, signal_number) && signal_number == SIGHUP)
                session->hungup_child = true;
        }
        if (signal_number == SIGKILL && !session->reaped)
            (void)kill(session->pid, signal_number); /* Also covers close-before-setsid. */
    }
}

void tt_runtime_request_close(tt_runtime *runtime, tt_session_id id) {
    tt_session *session = find_session(runtime, id);
    if (session == NULL || session->closing) return;
    session->closing = true;
    session->pending = 0;
    session->close_started = monotonic_now();
    if (!session->reaped) signal_session(session, SIGHUP);
}

static void flush_input(tt_session *session) {
    if (session->pending == 0 || session->input_fd < 0) return;
    ssize_t count = write(session->input_fd, session->input, session->pending);
    if (count > 0) {
        session->pending -= (size_t)count;
        memmove(session->input, session->input + count, session->pending);
    } else if (count < 0 && errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) {
        session->pending = 0;
        if (session->input_fd != session->output_fd) close(session->input_fd);
        session->input_fd = -1;
    }
}

static void check_exit(tt_session *session) {
    if (session->reaped) return;
    int status = 0;
    pid_t result = waitpid(session->pid, &status, WNOHANG);
    if (result == session->pid) {
        session->reaped = true;
        session->exit_code = WIFEXITED(status) ? WEXITSTATUS(status) :
                             WIFSIGNALED(status) ? 128 + WTERMSIG(status) : 125;
    } else if (result < 0 && errno == ECHILD) {
        session->reaped = true;
        session->exit_code = 125; /* Another owner consumed wait status. */
    }
}

void tt_runtime_pump(tt_runtime *runtime, tt_output_fn output, tt_exit_fn exited,
                     void *context) {
    if (runtime == NULL) return;
    size_t remaining = TT_FRAME_OUTPUT_LIMIT;
    unsigned char buffer[TT_READ_CHUNK];
    unsigned first = runtime->next_slot;
    runtime->next_slot = (first + 1) % TT_MAX_SESSIONS;
    for (unsigned offset = 0; offset < TT_MAX_SESSIONS; ++offset) {
        tt_session *session = &runtime->sessions[(first + offset) % TT_MAX_SESSIONS];
        if (session->id == 0) continue;
        check_exit(session);
        if (session->closing && !session->reaped) {
            double grace = session->defer_tty ? FROZEN_CLOSE_GRACE : CLOSE_GRACE;
            if (!session->forced && monotonic_now() - session->close_started >= grace) {
                signal_session(session, SIGKILL);
                session->forced = true;
                close_io(session);
            } else if (!session->forced) signal_session(session, SIGHUP);
        }
        if (!session->closing) flush_input(session);
        while (!session->eof && session->output_fd >= 0 && remaining > 0) {
            size_t capacity = sizeof(buffer);
            if (capacity > remaining) capacity = remaining;
            if (!session->closing && output != NULL) {
                size_t credit = TT_OUTPUT_CREDIT - session->outstanding;
                if (capacity > credit) capacity = credit;
            }
            if (capacity == 0) break;
            ssize_t count = read(session->output_fd, buffer, capacity);
            if (count > 0) {
                remaining -= (size_t)count;
                if (!session->closing && output != NULL) {
                    session->outstanding += (size_t)count;
                    output(context, session->id, buffer, (size_t)count);
                }
            } else if (count == 0 || (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK)) {
                close_io(session);
                break;
            } else break;
        }
        check_exit(session);
        if (session->reaped && session->closing) close_io(session);
        if (session->reaped && session->eof) {
            tt_session_id id = session->id;
            int exit_code = session->exit_code;
            close_io(session);
            session->id = 0;
            if (exited != NULL) exited(context, id, exit_code);
        }
    }
}

size_t tt_runtime_live_count(const tt_runtime *runtime) {
    size_t count = 0;
    if (runtime != NULL)
        for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i)
            if (runtime->sessions[i].id != 0) ++count;
    return count;
}

pid_t tt_runtime_foreground_pid(const tt_runtime *runtime, tt_session_id id) {
    if (runtime == NULL || id == 0) return -1;
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i) {
        const tt_session *session = &runtime->sessions[i];
        if (session->id == id && session->pty && session->output_fd >= 0 && !session->closing)
            return tcgetpgrp(session->output_fd);
    }
    return -1;
}

pid_t tt_runtime_child_pid(const tt_runtime *runtime, tt_session_id id) {
    if (runtime != NULL && id != 0)
        for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i)
            if (runtime->sessions[i].id == id) return runtime->sessions[i].pid;
    return -1;
}

void tt_runtime_destroy(tt_runtime *runtime) {
    if (runtime == NULL) return;
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i)
        if (runtime->sessions[i].id != 0)
            tt_runtime_request_close(runtime, runtime->sessions[i].id);
    double deadline = monotonic_now() + FROZEN_CLOSE_GRACE + 1.0;
    const struct timespec delay = {.tv_sec = 0, .tv_nsec = 5000000};
    while (tt_runtime_live_count(runtime) > 0 && monotonic_now() < deadline) {
        tt_runtime_pump(runtime, NULL, NULL, NULL);
        nanosleep(&delay, NULL);
    }
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i) {
        tt_session *session = &runtime->sessions[i];
        if (session->id != 0) {
            signal_session(session, SIGKILL);
            check_exit(session);
            close_io(session);
        }
    }
    free(runtime->helper);
    free(runtime);
}

int tt_runtime_child_main(int argc, char *argv[]) {
    if (argc < 6 || strcmp(argv[1], "--pty-child") != 0 ||
        strcmp(argv[4], "--") != 0 || argv[5][0] != '/' ||
        (strcmp(argv[2], "--claim-tty") != 0 &&
         strcmp(argv[2], "--defer-tty") != 0 && strcmp(argv[2], "--pipe") != 0)) {
        fprintf(stderr, "Tether: invalid child launch\n");
        return 125;
    }
    if (setsid() < 0 ||
        (strcmp(argv[2], "--claim-tty") == 0 && ioctl(STDIN_FILENO, TIOCSCTTY, 0) < 0) ||
        (argv[3][0] != '\0' && chdir(argv[3]) < 0)) {
        fprintf(stderr, "Tether: child setup failed: %s\n", strerror(errno));
        return 125;
    }
    execve(argv[5], &argv[5], environ);
    fprintf(stderr, "Tether: cannot execute %s: %s\n", argv[5], strerror(errno));
    return 127;
}
