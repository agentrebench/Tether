#include "tether_files.h"
#include <assert.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void write_file(const char *name, const char *data, size_t length) {
    int fd = open(name, O_WRONLY | O_CREAT | O_TRUNC, 0600);
    assert(fd >= 0);
    assert(write(fd, data, length) == (ssize_t)length);
    assert(close(fd) == 0);
}

int main(void) {
    char root[] = "/tmp/tether-files-XXXXXX";
    assert(mkdtemp(root));
    int original = open(".", O_RDONLY | O_DIRECTORY);
    assert(original >= 0 && chdir(root) == 0);
    assert(mkdir("src", 0700) == 0);
    assert(mkdir("empty", 0700) == 0);
    write_file("src/main.c", "int main(void) {}\n", 18);
    write_file("README.md", "# Hello\n", 8);
    write_file(".hidden", "hidden", 6);
    write_file("data.bin", "\0binary", 7);
    assert(symlink("/etc", "outside") == 0);
    assert(symlink("src", "inside") == 0);
    assert(symlink("..", "src/loop") == 0);
    assert(mkfifo("pipe", 0600) == 0);
    tt_file_result *result = calloc(1, sizeof(*result));
    assert(result);
    assert(tt_files_read(root, "", false, result) == 0);
    assert(result->kind == TT_FILE_DIRECTORY && result->count == 7);
    assert(!strcmp(result->entries[0].name, "empty"));
    assert(!strcmp(result->entries[1].name, "src"));
    assert(tt_files_read(root, "", true, result) == 0 && result->count == 8);
    assert(tt_files_read(root, "empty", true, result) == 0 && result->count == 0);
    assert(tt_files_read(root, "src/main.c", false, result) == 0);
    assert(result->kind == TT_FILE_TEXT && result->length == 18 && !result->truncated);
    assert(!memcmp(result->data, "int main(void) {}\n", 18));
    assert(tt_files_read(root, "data.bin", false, result) == 0 && result->kind == TT_FILE_BINARY);
    for (size_t i = 0; i < 3; ++i) {
        assert(tt_files_read(root, "../outside", false, result) != 0);
        assert(tt_files_read(root, "src/../../outside", false, result) != 0);
        assert(tt_files_read(root, "/etc/passwd", false, result) != 0);
        assert(tt_files_read(root, "outside/passwd", false, result) != 0);
        assert(tt_files_read(root, "inside/main.c", false, result) != 0);
        assert(tt_files_read(root, "src/loop/README.md", false, result) != 0);
        assert(tt_files_read(root, "missing", false, result) != 0);
        assert(tt_files_read(root, "pipe", false, result) != 0);
    }
    char *large = malloc(TT_FILE_BYTES + 20);
    assert(large); memset(large, 'a', TT_FILE_BYTES + 20);
    write_file("large.txt", large, TT_FILE_BYTES + 20); free(large);
    assert(tt_files_read(root, "large.txt", false, result) == 0);
    assert(result->length == TT_FILE_BYTES && result->truncated && result->size == TT_FILE_BYTES + 20);
    write_file("zero.txt", "", 0);
    assert(tt_files_read(root, "zero.txt", false, result) == 0 && result->length == 0 && !result->truncated);
    char name[32];
    for (size_t i = 0; i < TT_FILE_ENTRIES + 2; ++i) {
        snprintf(name, sizeof(name), "empty/%zu", i); write_file(name, "", 0);
    }
    assert(tt_files_read(root, "empty", false, result) == 0 && result->count == TT_FILE_ENTRIES && result->truncated);
    for (size_t i = 0; i < TT_FILE_ENTRIES + 2; ++i) {
        snprintf(name, sizeof(name), "empty/%zu", i); assert(unlink(name) == 0);
    }
    // A folder replaced by a link between successive requests is never followed.
    assert(rmdir("empty") == 0 && symlink("src", "empty") == 0);
    assert(tt_files_read(root, "empty/main.c", false, result) != 0);
    const char *files[] = {"src/main.c", "src/loop", "README.md", ".hidden", "data.bin", "outside", "inside", "pipe", "large.txt", "zero.txt", "empty"};
    for (size_t i = 0; i < sizeof(files) / sizeof(files[0]); ++i) assert(unlink(files[i]) == 0);
    assert(rmdir("src") == 0);
    free(result);
    assert(fchdir(original) == 0 && close(original) == 0 && rmdir(root) == 0);
    puts("File browser: bounded reads, directory sorting, hidden files, binary files, path and symlink isolation passed.");
    return 0;
}
