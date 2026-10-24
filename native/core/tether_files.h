#ifndef TETHER_FILES_H
#define TETHER_FILES_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

enum { TT_FILE_ENTRIES = 500, TT_FILE_BYTES = 256000, TT_FILE_PATH = 4096 };
typedef enum { TT_FILE_OTHER, TT_FILE_DIRECTORY, TT_FILE_TEXT, TT_FILE_BINARY, TT_FILE_LINK } tt_file_kind;
typedef struct {
    char name[256];
    tt_file_kind kind;
} tt_file_entry;
typedef struct {
    tt_file_kind kind;
    tt_file_entry entries[TT_FILE_ENTRIES];
    size_t count;
    unsigned char data[TT_FILE_BYTES + 1];
    size_t length;
    uint64_t size;
    bool truncated;
} tt_file_result;

// Caller owns result (allocate on the heap). No retained state; safe on a worker
// queue. Relative paths cannot escape root or traverse symlinks. Returns errno.
int tt_files_read(const char *root, const char *relative, bool hidden, tt_file_result *result);

#endif
