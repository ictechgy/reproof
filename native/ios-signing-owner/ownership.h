#ifndef REPRO_IOS_OWNERSHIP_H
#define REPRO_IOS_OWNERSHIP_H

#include <CoreFoundation/CoreFoundation.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>

typedef struct {
    int work_fd, producer_fd, phase_fd, live_fd, start_fd, termination_fd;
    char work_path[PATH_MAX], operation_id[65], request_digest[65];
    char context_digest[65], scope_digest[65], definition_digest[65];
    atomic_bool complete;
    atomic_int exit_code;
} ReproOwner;

static ReproOwner owner;

#ifdef REPRO_OWNER_CHILDREN
static pthread_mutex_t owner_child_lock = PTHREAD_MUTEX_INITIALIZER;
static pid_t owner_child_pid;

static void owner_stop_child(void) {
    pthread_mutex_lock(&owner_child_lock);
    if (owner_child_pid > 0) {
        /* Only this mutex-protected owner can reap or signal this child. */
        kill(owner_child_pid, SIGKILL);
        int status;
        pid_t waited;
        do { waited = waitpid(owner_child_pid, &status, 0); } while (waited < 0 && errno == EINTR);
        if (waited != owner_child_pid) {
            /* Preserve the locks if the original child's state is unknown. */
            for (;;) pause();
        }
        owner_child_pid = 0;
    }
    pthread_mutex_unlock(&owner_child_lock);
}

static int owner_poll_child(int *status) {
    pthread_mutex_lock(&owner_child_lock);
    pid_t waited = waitpid(owner_child_pid, status, WNOHANG);
    int result = waited > 0 && waited == owner_child_pid;
    if (result) owner_child_pid = 0;
    pthread_mutex_unlock(&owner_child_lock);
    return result;
}
#endif

static void owner_exit(int code) {
#ifdef REPRO_OWNER_CHILDREN
    owner_stop_child();
#endif
    _exit(code);
}

static int parse_fd(const char *text, int allow_zero) {
    if (!text[0] || strlen(text) > 7) return -1;
    for (const char *p = text; *p; ++p) if (*p < '0' || *p > '9') return -1;
    errno = 0;
    long value = strtol(text, NULL, 10);
    return !errno && ((allow_zero && value == 0) || (value >= 3 && value <= 1000000)) ? (int)value : -1;
}

static int private_fd(int descriptor, size_t maximum, int empty) {
    struct stat info;
    int flags = fcntl(descriptor, F_GETFL);
    return descriptor >= 3 && flags >= 0 && fstat(descriptor, &info) == 0 && S_ISREG(info.st_mode) &&
        info.st_uid == getuid() && info.st_nlink == 1 && (info.st_mode & 0777) == 0600 &&
        info.st_size >= 0 && (uint64_t)info.st_size <= maximum && (empty ? info.st_size == 0 : info.st_size > 0);
}

static CFDataRef read_private(int descriptor, size_t maximum) {
    if (!private_fd(descriptor, maximum, 0) || (fcntl(descriptor, F_GETFL) & O_ACCMODE) != O_RDONLY) return NULL;
    struct stat before, after;
    if (fstat(descriptor, &before)) return NULL;
    size_t size = (size_t)before.st_size;
    unsigned char *bytes = calloc(size, 1);
    if (!bytes) return NULL;
    size_t offset = 0;
    while (offset < size) {
        ssize_t count = pread(descriptor, bytes + offset, size - offset, (off_t)offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) break;
        offset += (size_t)count;
    }
    CFDataRef data = NULL;
    if (offset == size && !fstat(descriptor, &after) && before.st_size == after.st_size &&
        before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec &&
        before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec &&
        before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec &&
        before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec)
        data = CFDataCreate(NULL, bytes, (CFIndex)size);
    volatile unsigned char *clear = bytes;
    for (size_t index = 0; index < size; ++index) clear[index] = 0;
    free(bytes);
    return data;
}

static int field_string(CFDictionaryRef dictionary, CFStringRef key, char *output, size_t capacity) {
    CFTypeRef value = CFDictionaryGetValue(dictionary, key);
    if (!value || CFGetTypeID(value) != CFStringGetTypeID() || CFStringGetLength(value) == 0 ||
        (uint64_t)CFStringGetLength(value) >= capacity) return 0;
    for (CFIndex index = 0; index < CFStringGetLength(value); ++index)
        if (CFStringGetCharacterAtIndex(value, index) == 0) return 0;
    return CFStringGetCString(value, output, (CFIndex)capacity, kCFStringEncodingUTF8);
}

static int hex_digest(const char *text) {
    if (strlen(text) != 64) return 0;
    for (const char *p = text; *p; ++p) if (!(*p >= '0' && *p <= '9') && !(*p >= 'a' && *p <= 'f')) return 0;
    return 1;
}

static int owned_named(int descriptor, const char *name, int empty) {
    struct stat opened, named;
    return private_fd(descriptor, 4096, empty) && (fcntl(descriptor, F_GETFL) & O_ACCMODE) == O_RDWR &&
        !fstat(descriptor, &opened) && !fstatat(owner.work_fd, name, &named, AT_SYMLINK_NOFOLLOW) &&
        S_ISREG(named.st_mode) && opened.st_dev == named.st_dev && opened.st_ino == named.st_ino;
}

static int original_directory(void) {
    struct stat named, opened;
    return !lstat(owner.work_path, &named) && !fstat(owner.work_fd, &opened) && S_ISDIR(named.st_mode) &&
        S_ISDIR(opened.st_mode) && named.st_dev == opened.st_dev && named.st_ino == opened.st_ino &&
        opened.st_uid == getuid() && (opened.st_mode & 0777) == 0700;
}

static int owner_record(int descriptor, const char *state) {
    char buffer[2048];
    int length = snprintf(buffer, sizeof(buffer),
        "{\"schemaVersion\":1,\"operationId\":\"%s\",\"requestDigest\":\"%s\","
        "\"contextDigest\":\"%s\",\"scopeDigest\":\"%s\",\"definitionDigest\":\"%s\","
        "\"ownerPid\":%d,\"state\":\"%s\",\"recordMeaning\":\"%s\"}\n",
        owner.operation_id, owner.request_digest, owner.context_digest, owner.scope_digest,
        owner.definition_digest, getpid(), state, strcmp(state, "started") == 0 ? "owner-start" : "exit-intent");
    const char *name = descriptor == owner.start_fd ? "start.json" : "termination.json";
    if (length <= 0 || (size_t)length >= sizeof(buffer) || !original_directory() ||
        !owned_named(descriptor, name, 1) || !owned_named(owner.producer_fd, "producer.lock", 1) ||
        !owned_named(owner.phase_fd, "owner.lock", 1) || ftruncate(descriptor, 0)) return 0;
    size_t offset = 0;
    while (offset < (size_t)length) {
        ssize_t count = pwrite(descriptor, buffer + offset, (size_t)length - offset, (off_t)offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return 0;
        offset += (size_t)count;
    }
    return fsync(descriptor) == 0 && fsync(owner.work_fd) == 0;
}

static void *watch_parent(void *unused) {
    (void)unused;
    struct pollfd input = {.fd = owner.live_fd, .events = POLLIN | POLLHUP};
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now)) owner_exit(77);
    int64_t deadline = (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000 + 900000;
    int ready;
    for (;;) {
        if (clock_gettime(CLOCK_MONOTONIC, &now)) owner_exit(77);
        int64_t remaining = deadline - ((int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000);
        if (remaining <= 0) owner_exit(77);
        ready = poll(&input, 1, (int)remaining);
        if (ready >= 0 || errno != EINTR) break;
    }
    if (ready <= 0) owner_exit(77);
    unsigned char byte = 0;
    ssize_t count;
    do { count = read(owner.live_fd, &byte, 1); } while (count < 0 && errno == EINTR);
    if (count == 1 && byte == 1 && atomic_load(&owner.complete)) owner_exit(atomic_load(&owner.exit_code));
    owner_exit(count == 0 ? 75 : 76);
    return NULL;
}

static int start_liveness(int descriptor) {
    struct rlimit core = {0, 0};
    if (setrlimit(RLIMIT_CORE, &core)) return 0;
    struct stat live;
    if (fstat(descriptor, &live) || !S_ISFIFO(live.st_mode) || live.st_uid != getuid() ||
        (fcntl(descriptor, F_GETFL) & O_ACCMODE) != O_RDONLY) return 0;
    owner.live_fd = descriptor;
    pthread_t watcher;
    return pthread_create(&watcher, NULL, watch_parent, NULL) == 0 && pthread_detach(watcher) == 0;
}

static int start_owner(CFDictionaryRef config, const int descriptors[9]) {
    owner.work_fd = descriptors[1]; owner.producer_fd = descriptors[2]; owner.phase_fd = descriptors[3];
    owner.start_fd = descriptors[7]; owner.termination_fd = descriptors[8];
    if (!field_string(config, CFSTR("workPath"), owner.work_path, sizeof(owner.work_path)) ||
        !field_string(config, CFSTR("operationId"), owner.operation_id, sizeof(owner.operation_id)) ||
        !field_string(config, CFSTR("requestDigest"), owner.request_digest, sizeof(owner.request_digest)) ||
        !field_string(config, CFSTR("contextDigest"), owner.context_digest, sizeof(owner.context_digest)) ||
        !field_string(config, CFSTR("scopeDigest"), owner.scope_digest, sizeof(owner.scope_digest)) ||
        !field_string(config, CFSTR("definitionDigest"), owner.definition_digest, sizeof(owner.definition_digest))) return 0;
    if (owner.operation_id[0] < 'a' || owner.operation_id[0] > 'z') return 0;
    for (const char *p = owner.operation_id; *p; ++p)
        if (!(*p >= 'a' && *p <= 'z') && !(*p >= '0' && *p <= '9') && *p != '-' && *p != '_') return 0;
    if (!hex_digest(owner.request_digest) || !hex_digest(owner.context_digest) ||
        !hex_digest(owner.scope_digest) || !hex_digest(owner.definition_digest)) return 0;
    char canonical[PATH_MAX];
    if (owner.work_path[0] != '/' || !realpath(owner.work_path, canonical) || strcmp(canonical, owner.work_path) ||
        !original_directory() || !owned_named(owner.producer_fd, "producer.lock", 1) ||
        !owned_named(owner.phase_fd, "owner.lock", 1) || !owned_named(owner.start_fd, "start.json", 1) ||
        !owned_named(owner.termination_fd, "termination.json", 1)) return 0;
    if (flock(owner.producer_fd, LOCK_EX | LOCK_NB) || flock(owner.phase_fd, LOCK_EX | LOCK_NB)) return 0;
    return owner_record(owner.start_fd, "started");
}

static void finish_owner(int exit_code, unsigned int objects) {
    if (!owner_record(owner.termination_fd, exit_code == 0 ? "succeeded" : "failed")) owner_exit(74);
    atomic_store(&owner.exit_code, exit_code);
    atomic_store(&owner.complete, true);
    printf("{\"schemaVersion\":1,\"contextDigest\":\"%s\",\"status\":\"%s\",\"signedCodeObjects\":%u}\n",
        owner.context_digest, exit_code == 0 ? "succeeded" : "failed", objects);
    if (fflush(stdout)) owner_exit(74);
    struct timespec delay = {.tv_sec = 0, .tv_nsec = 10000000};
    for (unsigned int count = 0; count < 500; ++count) nanosleep(&delay, NULL);
    owner_exit(76);
}

#endif
