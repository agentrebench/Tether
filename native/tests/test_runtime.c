#include "tether_runtime.h"
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

static char executable[PATH_MAX];
static unsigned checks;
#define CHECK(expression) do { ++checks; if (!(expression)) { \
    fprintf(stderr, "%s:%d: %s (errno=%d %s)\n", __FILE__, __LINE__, \
            #expression, errno, strerror(errno)); abort(); } } while (0)

typedef struct {
    tt_runtime *runtime;
    char bytes[32768];
    size_t length;
    size_t total;
    size_t frame;
    unsigned exits;
    int exit_code;
    bool acknowledge;
} capture;

static double now(void) {
    struct timespec value;
    clock_gettime(CLOCK_MONOTONIC, &value);
    return (double)value.tv_sec + (double)value.tv_nsec / 1e9;
}

static void on_output(void *context, tt_session_id id,
                       const unsigned char *bytes, size_t count) {
    capture *result = context;
    size_t saved = count;
    if (saved > sizeof(result->bytes) - result->length - 1)
        saved = sizeof(result->bytes) - result->length - 1;
    memcpy(result->bytes + result->length, bytes, saved);
    result->length += saved;
    result->bytes[result->length] = '\0';
    result->total += count;
    result->frame += count;
    CHECK(result->frame <= TT_FRAME_OUTPUT_LIMIT);
    if (result->acknowledge) tt_runtime_ack(result->runtime, id, count);
}

static void on_exit(void *context, tt_session_id id, int code) {
    (void)id;
    capture *result = context;
    ++result->exits;
    result->exit_code = code;
}

static void pump(capture *result) {
    result->frame = 0;
    tt_runtime_pump(result->runtime, on_output, on_exit, result);
    const struct timespec delay = {.tv_sec = 0, .tv_nsec = 2000000};
    nanosleep(&delay, NULL);
}

static void await_text(capture *result, const char *text) {
    double deadline = now() + 4;
    while (strstr(result->bytes, text) == NULL && now() < deadline) pump(result);
    if (strstr(result->bytes, text) == NULL) fprintf(stderr, "Output: %s\n", result->bytes);
    CHECK(strstr(result->bytes, text) != NULL);
}

static void await_done(capture *result) {
    double deadline = now() + 5;
    while (tt_runtime_live_count(result->runtime) && now() < deadline) pump(result);
    CHECK(tt_runtime_live_count(result->runtime) == 0);
    for (unsigned i = 0; i < 3; ++i) pump(result);
}

static tt_session_id launch(capture *result, const char *const *argv,
                             bool pty, bool defer, const char *cwd) {
    tt_launch request = {.argv = argv, .cwd = cwd, .cols = 80, .rows = 24,
                         .pty = pty, .defer_tty = defer};
    char error[256];
    tt_session_id id = tt_runtime_spawn(result->runtime, &request, error, sizeof(error));
    if (!id) fprintf(stderr, "Spawn: %s\n", error);
    CHECK(id > 0);
    return id;
}

static capture begin(void) {
    capture result = {.runtime = tt_runtime_create(executable), .acknowledge = true};
    CHECK(result.runtime != NULL);
    return result;
}

static void send_text(capture *result, tt_session_id id, const char *text) {
    CHECK(tt_runtime_write(result->runtime, id, text, strlen(text)));
}

static void check_reaped(pid_t pid) {
    int status;
    errno = 0;
    CHECK(waitpid(pid, &status, WNOHANG) == -1 && errno == ECHILD);
}

static void test_shell(void) {
    capture result = begin();
    const char *const argv[] = {"/bin/zsh", "-f", "-i", NULL};
    tt_session_id id = launch(&result, argv, true, false, "/private/tmp");
    pid_t pid = tt_runtime_child_pid(result.runtime, id);
    send_text(&result, id, "printf '\\nPTY_%s\\n' READY\n");
    await_text(&result, "PTY_READY");
    CHECK(tt_runtime_foreground_pid(result.runtime, id) == pid);
    CHECK(tt_runtime_foreground_pid(result.runtime, 0) == -1);
    CHECK(tt_runtime_resize(result.runtime, id, 93, 37));
    CHECK(!tt_runtime_resize(result.runtime, id, 0, 37));
    send_text(&result, id, "stty size\n");
    await_text(&result, "37 93");
    send_text(&result, id, "sleep 30\n");
    for (unsigned i = 0; i < 30; ++i) pump(&result);
    send_text(&result, id, "\003");
    send_text(&result, id, "printf '\\nINT_%s\\n' SURVIVED\n");
    await_text(&result, "INT_SURVIVED");
    send_text(&result, id, "exit 7\n");
    await_done(&result);
    CHECK(result.exits == 1 && result.exit_code == 7);
    CHECK(tt_runtime_foreground_pid(result.runtime, id) == -1);
    check_reaped(pid);
    tt_runtime_destroy(result.runtime);
}

static void test_arguments_and_fds(void) {
    char directory[] = "/private/tmp/tether native spaces.XXXXXX";
    CHECK(mkdtemp(directory) != NULL);
    char link[PATH_MAX];
    CHECK(snprintf(link, sizeof(link), "%s/child with spaces", directory) > 0);
    CHECK(symlink(executable, link) == 0);
    int leaked = open("/dev/null", O_RDONLY); /* Deliberately not CLOEXEC. */
    CHECK(leaked > 2);
    capture result = begin();
    const char *const argv[] = {link, "--fixture-argv", "literal $(touch BAD); `id`", NULL};
    tt_session_id id = launch(&result, argv, false, false, directory);
    CHECK(!tt_runtime_resize(result.runtime, id, 80, 24));
    CHECK(tt_runtime_foreground_pid(result.runtime, id) == -1);
    await_done(&result);
    CHECK(result.exits == 1 && result.exit_code == 0);
    CHECK(strstr(result.bytes, "literal $(touch BAD); `id`") != NULL);
    CHECK(strstr(result.bytes, directory) != NULL);
    CHECK(strstr(result.bytes, "extra_fds=0") != NULL);
    close(leaked);
    tt_runtime_destroy(result.runtime);
    unlink(link);
    CHECK(rmdir(directory) == 0);
}

static void test_pipe_input(void) {
    capture result = begin();
    const char *const argv[] = {executable, "--fixture-echo", NULL};
    tt_session_id id = launch(&result, argv, false, false, NULL);
    send_text(&result, id, "PIPE_INPUT\n");
    await_done(&result);
    CHECK(strcmp(result.bytes, "PIPE_INPUT\n") == 0);
    CHECK(result.exits == 1 && result.exit_code == 0);
    tt_runtime_destroy(result.runtime);
}

static void test_backpressure(void) {
    capture result = begin();
    result.acknowledge = false;
    const char *const argv[] = {executable, "--fixture-flood", NULL};
    tt_session_id id = launch(&result, argv, false, false, NULL);
    double deadline = now() + 4;
    while (result.total < TT_OUTPUT_CREDIT && now() < deadline) pump(&result);
    CHECK(result.total == TT_OUTPUT_CREDIT);
    for (unsigned i = 0; i < 20; ++i) pump(&result);
    CHECK(result.total == TT_OUTPUT_CREDIT && result.exits == 0);
    tt_runtime_ack(result.runtime, id, TT_OUTPUT_CREDIT);
    result.acknowledge = true;
    await_done(&result);
    CHECK(result.total == 6 * TT_OUTPUT_CREDIT);
    CHECK(result.exits == 1 && result.exit_code == 42);
    tt_runtime_destroy(result.runtime);
}

static void test_close_and_limits(void) {
    capture result = begin();
    const char *const argv[] = {executable, "--fixture-ignore-hup", NULL};
    tt_session_id ids[TT_MAX_SESSIONS];
    pid_t pids[TT_MAX_SESSIONS];
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i) {
        ids[i] = launch(&result, argv, i % 2 == 0, false, NULL);
        pids[i] = tt_runtime_child_pid(result.runtime, ids[i]);
        if (i) CHECK(ids[i] > ids[i - 1]);
    }
    char error[128];
    tt_launch request = {.argv = argv};
    CHECK(tt_runtime_spawn(result.runtime, &request, error, sizeof(error)) == 0);
    CHECK(errno == EUSERS && error[0]);
    unsigned char bytes[TT_INPUT_LIMIT] = {0};
    CHECK(tt_runtime_write(result.runtime, ids[0], bytes, sizeof(bytes)));
    CHECK(!tt_runtime_write(result.runtime, ids[0], bytes, 1));
    CHECK(!tt_runtime_write(result.runtime, ids[0], bytes, SIZE_MAX));
    CHECK(!tt_runtime_write(result.runtime, UINT64_MAX, bytes, 1));
    /* Close immediately, including children not yet through setsid(). */
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i) {
        tt_runtime_request_close(result.runtime, ids[i]);
        tt_runtime_request_close(result.runtime, ids[i]);
        CHECK(!tt_runtime_write(result.runtime, ids[i], bytes, 1));
    }
    await_done(&result);
    CHECK(result.exits == TT_MAX_SESSIONS);
    for (unsigned i = 0; i < TT_MAX_SESSIONS; ++i) check_reaped(pids[i]);
    tt_session_id again = launch(&result, argv, true, false, NULL);
    CHECK(again > ids[TT_MAX_SESSIONS - 1]);
    pid_t last_pid = tt_runtime_child_pid(result.runtime, again);
    double started = now();
    tt_runtime_destroy(result.runtime);
    CHECK(now() - started < 3.2);
    check_reaped(last_pid);
}

static void test_forced_close(void) {
    capture result = begin();
    const char *const argv[] = {executable, "--fixture-ignore-hup", NULL};
    tt_session_id id = launch(&result, argv, true, false, NULL);
    pid_t pid = tt_runtime_child_pid(result.runtime, id);
    await_text(&result, "READY");
    tt_runtime_request_close(result.runtime, id);
    await_done(&result);
    CHECK(result.exits == 1 && result.exit_code == 128 + SIGKILL);
    check_reaped(pid);
    tt_runtime_destroy(result.runtime);
}

static void test_frozen_cleanup(void) {
    char marker[] = "/private/tmp/tether-reaped.XXXXXX";
    int fd = mkstemp(marker);
    CHECK(fd >= 0); close(fd); unlink(marker);
    capture result = begin();
    const char *const argv[] = {executable, "--fixture-bootloader", marker, NULL};
    tt_session_id id = launch(&result, argv, true, true, NULL);
    pid_t pid = tt_runtime_child_pid(result.runtime, id);
    await_text(&result, "PYTHON_READY");
    tt_runtime_request_close(result.runtime, id);
    await_done(&result);
    CHECK(result.exits == 1 && result.exit_code == 0);
    CHECK(access(marker, F_OK) == 0);
    unlink(marker);
    check_reaped(pid);
    tt_runtime_destroy(result.runtime);
}

static void test_failed_launch(void) {
    CHECK(tt_runtime_create("relative") == NULL);
    capture result = begin();
    const char *const bad[] = {"/does/not/exist", NULL};
    tt_launch request = {.argv = bad};
    char error[128];
    CHECK(tt_runtime_spawn(result.runtime, &request, error, sizeof(error)) == 0);
    CHECK(error[0] && tt_runtime_live_count(result.runtime) == 0);
    const char *const good[] = {executable, "--fixture-echo", NULL};
    request.argv = good; request.cwd = "/not/a/directory";
    CHECK(tt_runtime_spawn(result.runtime, &request, error, sizeof(error)) == 0);
    request.cwd = NULL; request.pty = true; request.cols = 0; request.rows = 24;
    CHECK(tt_runtime_spawn(result.runtime, &request, error, sizeof(error)) == 0);
    tt_runtime_destroy(result.runtime);
}

static void test_environment_and_exec_failure(void) {
    capture result = begin();
    const char *const argv[] = {"/usr/bin/env", NULL};
    /* ASan's exec helper needs an absolute PATH to find macOS atos even with
       this deliberately minimal child environment. */
    const char *const envp[] = {"TETHER_LITERAL=value with $(literal); spaces", "PATH=/usr/bin:/bin", NULL};
    tt_launch request = {.argv = argv, .envp = envp};
    char error[128];
    CHECK(tt_runtime_spawn(result.runtime, &request, error, sizeof(error)) > 0);
    await_done(&result);
    CHECK(strcmp(result.bytes, "TETHER_LITERAL=value with $(literal); spaces\nPATH=/usr/bin:/bin\n") == 0);
    CHECK(result.exits == 1 && result.exit_code == 0);
    tt_runtime_destroy(result.runtime);

    char filename[] = "/private/tmp/tether-not-executable.XXXXXX";
    int fd = mkstemp(filename);
    CHECK(fd >= 0);
    const char invalid[] = "this is not an executable or a shell script";
    CHECK(write(fd, invalid, sizeof(invalid) - 1) == sizeof(invalid) - 1);
    CHECK(fchmod(fd, 0700) == 0);
    close(fd);
    result = begin();
    const char *const invalid_argv[] = {filename, NULL};
    launch(&result, invalid_argv, false, false, NULL);
    await_done(&result);
    CHECK(result.exits == 1 && result.exit_code == 127);
    CHECK(strstr(result.bytes, "cannot execute") != NULL);
    tt_runtime_destroy(result.runtime);
    unlink(filename);
}

static int fixture(int argc, char *argv[]) {
    if (strcmp(argv[1], "--fixture-argv") == 0 && argc == 3) {
        char cwd[PATH_MAX];
        if (getcwd(cwd, sizeof(cwd)) == NULL) return 1;
        unsigned fds = 0;
        for (int fd = 3; fd < 1024; ++fd) if (fcntl(fd, F_GETFD) >= 0) ++fds;
        printf("%s\n%s\nextra_fds=%u\n", argv[2], cwd, fds);
        return 0;
    }
    if (strcmp(argv[1], "--fixture-echo") == 0) {
        char bytes[64];
        ssize_t size = read(STDIN_FILENO, bytes, sizeof(bytes));
        return size > 0 && write(STDOUT_FILENO, bytes, (size_t)size) == size ? 0 : 1;
    }
    if (strcmp(argv[1], "--fixture-flood") == 0) {
        char bytes[4096]; memset(bytes, 'x', sizeof(bytes));
        for (unsigned i = 0; i < 6 * TT_OUTPUT_CREDIT / sizeof(bytes); ++i) {
            size_t offset = 0;
            while (offset < sizeof(bytes)) {
                ssize_t count = write(STDOUT_FILENO, bytes + offset, sizeof(bytes) - offset);
                if (count < 0) return 1;
                offset += (size_t)count;
            }
        }
        return 42;
    }
    if (strcmp(argv[1], "--fixture-ignore-hup") == 0) {
        signal(SIGHUP, SIG_IGN);
        puts("READY"); fflush(stdout);
        for (;;) pause();
    }
    if (strcmp(argv[1], "--fixture-bootloader") == 0 && argc == 3) {
        pid_t child = fork();
        if (child < 0) return 1;
        if (child == 0) {
            if (setsid() < 0 || ioctl(0, TIOCSCTTY, 0) < 0) _exit(2);
            puts("PYTHON_READY"); fflush(stdout);
            for (;;) pause();
        }
        int status;
        if (waitpid(child, &status, 0) != child) return 3;
        int fd = open(argv[2], O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd < 0) return 4;
        close(fd);
        return WIFSIGNALED(status) && WTERMSIG(status) == SIGHUP ? 0 : 5;
    }
    return 99;
}

int main(int argc, char *argv[]) {
    if (argc > 1 && strcmp(argv[1], "--pty-child") == 0)
        return tt_runtime_child_main(argc, argv);
    if (argc > 1) return fixture(argc, argv);
    CHECK(realpath(argv[0], executable) != NULL);
    test_failed_launch();
    test_shell();
    test_arguments_and_fds();
    test_pipe_input();
    test_backpressure();
    test_close_and_limits();
    test_forced_close();
    test_frozen_cleanup();
    test_environment_and_exec_failure();
    printf("{\"suite\":\"native_runtime\",\"tests\":9,\"checks\":%u,\"passed\":true}\n", checks);
    return 0;
}
