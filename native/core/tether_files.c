#include "tether_files.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>

static tt_file_kind kind(mode_t mode) {
    if (S_ISDIR(mode)) return TT_FILE_DIRECTORY;
    if (S_ISREG(mode)) return TT_FILE_TEXT;
    if (S_ISLNK(mode)) return TT_FILE_LINK;
    return TT_FILE_OTHER;
}

static int compare(const void *left, const void *right) {
    const tt_file_entry *a = left, *b = right;
    if ((a->kind == TT_FILE_DIRECTORY) != (b->kind == TT_FILE_DIRECTORY))
        return a->kind == TT_FILE_DIRECTORY ? -1 : 1;
    int order = strcasecmp(a->name, b->name);
    return order ? order : strcmp(a->name, b->name);
}

static int list(int fd, bool hidden, tt_file_result *result) {
    DIR *directory = fdopendir(fd);
    if (!directory) { int error = errno; close(fd); return error; }
    int error = 0;
    for (size_t scanned = 0; scanned <= 5000; ++scanned) {
        errno = 0;
        struct dirent *entry = readdir(directory);
        if (!entry) { error = errno; break; }
        if (scanned == 5000) { result->truncated = true; break; }
        if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
        if (!hidden && entry->d_name[0] == '.') continue;
        if (result->count == TT_FILE_ENTRIES) { result->truncated = true; break; }
        tt_file_entry *row = &result->entries[result->count++];
        size_t length = strlen(entry->d_name);
        if (length >= sizeof(row->name)) { --result->count; result->truncated = true; continue; }
        memcpy(row->name, entry->d_name, length + 1);
        struct stat info;
        row->kind = fstatat(fd, row->name, &info, AT_SYMLINK_NOFOLLOW) ? TT_FILE_OTHER : kind(info.st_mode);
    }
    closedir(directory);
    qsort(result->entries, result->count, sizeof(result->entries[0]), compare);
    return error;
}

int tt_files_read(const char *root, const char *relative, bool hidden, tt_file_result *result) {
    if (!root || !relative || !result || relative[0] == '/' || strlen(relative) >= TT_FILE_PATH)
        return EINVAL;
    memset(result, 0, sizeof(*result));
    char path[TT_FILE_PATH];
    memcpy(path, relative, strlen(relative) + 1);
    int fd = open(root, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return errno;
    char *save = NULL;
    for (char *part = strtok_r(path, "/", &save); part; part = strtok_r(NULL, "/", &save)) {
        if (!strcmp(part, ".") || !strcmp(part, "..")) { close(fd); return EPERM; }
        struct stat info;
        if (fstatat(fd, part, &info, AT_SYMLINK_NOFOLLOW)) { int error = errno; close(fd); return error; }
        if (!S_ISDIR(info.st_mode) && !S_ISREG(info.st_mode)) { close(fd); return EPERM; }
        int next = openat(fd, part, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
        int error = errno;
        close(fd);
        if (next < 0) return error;
        fd = next;
    }
    struct stat info;
    if (fstat(fd, &info)) { int error = errno; close(fd); return error; }
    result->kind = kind(info.st_mode);
    if (result->kind == TT_FILE_DIRECTORY) return list(fd, hidden, result);
    if (result->kind != TT_FILE_TEXT) { close(fd); return EPERM; }
    result->size = info.st_size > 0 ? (uint64_t)info.st_size : 0;
    int error = 0;
    unsigned interrupts = 0;
    while (result->length < sizeof(result->data)) {
        ssize_t count = read(fd, result->data + result->length, sizeof(result->data) - result->length);
        if (count < 0 && errno == EINTR && ++interrupts < 4) continue;
        if (count < 0) { error = errno; break; }
        if (!count) break;
        result->length += (size_t)count;
    }
    close(fd);
    result->truncated = result->length > TT_FILE_BYTES;
    if (result->truncated) result->length = TT_FILE_BYTES;
    if (memchr(result->data, 0, result->length)) result->kind = TT_FILE_BINARY;
    return error;
}
