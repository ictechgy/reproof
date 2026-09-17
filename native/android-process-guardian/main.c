/* Process guardian for fixed scoped Android SDK clients.
 * The command is constructed by trusted adapter code, never issue/AI input.
 * Only a sandbox-exec command can be launched. No signing material is passed.
 */
#define REPRO_OWNER_CHILDREN 1
#include "ownership.h"
#include <CommonCrypto/CommonDigest.h>

static volatile sig_atomic_t interrupted;
static void interrupted_signal(int number) { (void)number; interrupted = 1; }

static int same_digest(CFDataRef body, const char *expected) {
    unsigned char hash[CC_SHA256_DIGEST_LENGTH]; char hex[65];
    CC_SHA256(CFDataGetBytePtr(body), (CC_LONG)CFDataGetLength(body), hash);
    for (size_t index = 0; index < sizeof(hash); ++index) snprintf(hex+2*index, 3, "%02x", hash[index]);
    return strcmp(hex, expected) == 0;
}

static int original_lock(int descriptor, int directory, const char *name, int empty) {
    struct stat parent, opened, named;
    if (fstat(directory, &parent) || !S_ISDIR(parent.st_mode) || parent.st_uid != getuid() ||
        (parent.st_mode & 0777) != 0700 || !private_fd(descriptor, 64, empty) ||
        (fcntl(descriptor, F_GETFL) & O_ACCMODE) != O_RDWR || fstat(descriptor, &opened) ||
        fstatat(directory, name, &named, AT_SYMLINK_NOFOLLOW) || !S_ISREG(named.st_mode) ||
        opened.st_dev != named.st_dev || opened.st_ino != named.st_ino) return 0;
    int probe = openat(directory, name, O_RDWR | O_NOFOLLOW | O_NONBLOCK);
    if (probe < 0) return 0;
    int locked = flock(probe, LOCK_EX | LOCK_NB);
    int held = locked != 0 && (errno == EWOULDBLOCK || errno == EAGAIN);
    close(probe);
    return held && flock(descriptor, LOCK_EX | LOCK_NB) == 0;
}

static int original_locks(void) {
    char device_name[80];
    if (!hex_digest(owner.scope_digest)) return 0;
    snprintf(device_name, sizeof(device_name), "%s.lock", owner.scope_digest);
    return original_lock(owner.producer_fd, producer_directory_fd, "producer.lock", 1) &&
        original_lock(owner.phase_fd, device_directory_fd, device_name, 0);
}

static int regular_digest(const char *path, const char *expected, size_t maximum, int executable) {
    int descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    if (descriptor < 0) return 0;
    struct stat before, after;
    int good = !fstat(descriptor, &before) && S_ISREG(before.st_mode) && before.st_nlink == 1 &&
        (before.st_uid == 0 || before.st_uid == getuid()) && !(before.st_mode & 0022) &&
        (!executable || (before.st_mode & 0111)) && before.st_size > 0 && (uint64_t)before.st_size <= maximum;
    CC_SHA256_CTX digest; CC_SHA256_Init(&digest);
    unsigned char buffer[16384], result[CC_SHA256_DIGEST_LENGTH]; size_t total = 0;
    while (good) {
        ssize_t count = read(descriptor, buffer, sizeof(buffer));
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) { good = 0; break; }
        if (!count) break;
        total += (size_t)count;
        if (total > (size_t)before.st_size) { good = 0; break; }
        CC_SHA256_Update(&digest, buffer, (CC_LONG)count);
    }
    good = good && total == (size_t)before.st_size && !fstat(descriptor, &after) &&
        before.st_size == after.st_size && before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec &&
        before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec &&
        before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec;
    close(descriptor); CC_SHA256_Final(result, &digest);
    char hex[65];
    for (size_t index = 0; index < sizeof(result); ++index) snprintf(hex+2*index, 3, "%02x", result[index]);
    return good && strcmp(hex, expected) == 0;
}

static int file_digest(const char *path, const char *expected) {
    return regular_digest(path, expected, 64*1024*1024, 1);
}

static int identifier(const char *value) {
    if (!value[0] || value[0] < 'a' || value[0] > 'z' || strlen(value) > 64) return 0;
    for (const char *p = value; *p; ++p)
        if (!(*p >= 'a' && *p <= 'z') && !(*p >= '0' && *p <= '9') && *p != '_' && *p != '-') return 0;
    return 1;
}

static int fixed_inspector(CFDictionaryRef config, CFArrayRef command, char **args) {
    char kind[32], tool[PATH_MAX], tool_hash[65], apk[PATH_MAX], apk_hash[65], bytes[24], work[PATH_MAX];
    if (CFArrayGetCount(command) != 7 || strcmp(args[4], "dump") || strcmp(args[5], "badging") ||
        !field_string(config, CFSTR("toolKind"), kind, sizeof(kind)) || strcmp(kind, "apk-inspector") ||
        !field_string(config, CFSTR("packageInspectorPath"), tool, sizeof(tool)) || strcmp(tool, args[3]) ||
        !field_string(config, CFSTR("packageInspectorSha256"), tool_hash, sizeof(tool_hash)) || !hex_digest(tool_hash) ||
        !field_string(config, CFSTR("apkPath"), apk, sizeof(apk)) || strcmp(apk, args[6]) ||
        !field_string(config, CFSTR("apkSha256"), apk_hash, sizeof(apk_hash)) || !hex_digest(apk_hash) ||
        !field_string(config, CFSTR("apkBytes"), bytes, sizeof(bytes)) ||
        !field_string(config, CFSTR("childWorkPath"), work, sizeof(work))) return 0;
    errno = 0; char *end = NULL; unsigned long long size = strtoull(bytes, &end, 10);
    if (errno || !end || *end || bytes[0] == '0' || size < 1 || size > 150*1024*1024) return 0;
    for (const char *p = bytes; *p; ++p) if (*p < '0' || *p > '9') return 0;
    const char *names[] = {"candidate.apk", "original.apk", "helper.apk"};
    int selected = 0; char expected[PATH_MAX];
    for (unsigned index = 0; index < 3; ++index) {
        snprintf(expected, sizeof(expected), "%s/%s", work, names[index]);
        if (!strcmp(apk, expected)) selected = 1;
    }
    struct stat info;
    if (!selected || lstat(apk, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid() ||
        info.st_nlink != 1 || (info.st_mode & 0777) != 0600 || (uint64_t)info.st_size != size ||
        !regular_digest(apk, apk_hash, 150*1024*1024, 0) || !file_digest(tool, tool_hash)) return 0;
    CFArrayRef support = CFDictionaryGetValue(config, CFSTR("packageInspectorSupport"));
    if (!support || CFGetTypeID(support) != CFArrayGetTypeID() || CFArrayGetCount(support) > 1) return 0;
    for (CFIndex index = 0; index < CFArrayGetCount(support); ++index) {
        CFDictionaryRef item = CFArrayGetValueAtIndex(support, index);
        char path[PATH_MAX], digest[65], parent[PATH_MAX];
        if (!item || CFGetTypeID(item) != CFDictionaryGetTypeID() || CFDictionaryGetCount(item) != 2 ||
            !field_string(item, CFSTR("path"), path, sizeof(path)) ||
            !field_string(item, CFSTR("sha256"), digest, sizeof(digest)) || !hex_digest(digest)) return 0;
        strlcpy(parent, tool, sizeof(parent)); char *slash = strrchr(parent, '/');
        if (!slash) return 0;
        *slash = 0; snprintf(expected, sizeof(expected), "%s/lib64/libc++.dylib", parent);
        if (strcmp(path, expected) || !regular_digest(path, digest, 64*1024*1024, 0)) return 0;
    }
    return 1;
}

static int fixed_command(CFDictionaryRef config, CFArrayRef command, char **args, int input) {
    char adb[PATH_MAX], adb_hash[65], sandbox_hash[65], input_hash[65], binding[65], generation[24], host[65], helper[65];
    int inspector = CFDictionaryContainsKey(config, CFSTR("toolKind"));
    if (CFArrayGetCount(command) < 4 || strcmp(args[0], "/usr/bin/sandbox-exec") || strcmp(args[1], "-p") ||
        !field_string(config, CFSTR("adbPath"), adb, sizeof(adb)) ||
        !field_string(config, CFSTR("adbSha256"), adb_hash, sizeof(adb_hash)) || !hex_digest(adb_hash) ||
        !field_string(config, CFSTR("sandboxSha256"), sandbox_hash, sizeof(sandbox_hash)) || !hex_digest(sandbox_hash) ||
        !field_string(config, CFSTR("stdinDigest"), input_hash, sizeof(input_hash)) || !hex_digest(input_hash) ||
        !field_string(config, CFSTR("nativeBindingDigest"), binding, sizeof(binding)) || !hex_digest(binding) ||
        !field_string(config, CFSTR("ownershipGeneration"), generation, sizeof(generation)) ||
        !field_string(config, CFSTR("hostIncarnation"), host, sizeof(host)) || !identifier(host) ||
        !field_string(config, CFSTR("helperIncarnation"), helper, sizeof(helper)) || !identifier(helper)) return 0;
    errno = 0; char *end = NULL; unsigned long long value = strtoull(generation, &end, 10);
    if (errno || !end || *end || value == 0 || value > INT64_MAX || generation[0] == '0') return 0;
    for (const char *p = generation; *p; ++p) if (*p < '0' || *p > '9') return 0;
    if (inspector) {
        if (!fixed_inspector(config, command, args)) return 0;
    } else {
    if (CFArrayGetCount(command) < 9 || strcmp(args[3], adb) ||
        strcmp(args[4], "-L") || strncmp(args[5], "localfilesystem:/", 17) || strcmp(args[6], "-s") ||
        !args[7][0] || strlen(args[7]) > 256 ||
        (strcmp(args[8], "devices") && strcmp(args[8], "shell") && strcmp(args[8], "exec-out") && strcmp(args[8], "install"))) return 0;
    for (const char *p = args[7]; *p; ++p) if ((unsigned char)*p <= 32 || (unsigned char)*p == 127) return 0;
    }
    if (!file_digest(adb, adb_hash) || !file_digest("/usr/bin/sandbox-exec", sandbox_hash)) return 0;
    struct stat info;
    if (fstat(input, &info) || (fcntl(input, F_GETFL) & O_ACCMODE) != O_RDONLY ||
        (!private_fd(input, 64*1024, 0) && !private_fd(input, 64*1024, 1)) || (inspector && info.st_size != 0)) return 0;
    CFDataRef body = info.st_size ? read_private(input, 64*1024) : CFDataCreate(NULL, NULL, 0);
    int good = body && same_digest(body, input_hash);
    if (body) CFRelease(body);
    return good;
}

static int save_result(int descriptor, int return_code, const unsigned char *out, size_t out_size,
                       const unsigned char *error, size_t error_size, int bounded) {
    CFNumberRef code = CFNumberCreate(NULL, kCFNumberIntType, &return_code);
    CFDataRef stdout_data = CFDataCreate(NULL, out, (CFIndex)out_size);
    CFDataRef stderr_data = CFDataCreate(NULL, error, (CFIndex)error_size);
    if (!code || !stdout_data || !stderr_data) return 0;
    const void *keys[] = {CFSTR("returnCode"), CFSTR("stdout"), CFSTR("stderr"), CFSTR("bounded")};
    const void *values[] = {code, stdout_data, stderr_data, bounded ? kCFBooleanTrue : kCFBooleanFalse};
    CFDictionaryRef result = CFDictionaryCreate(NULL, keys, values, 4,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDataRef encoded = result ? CFPropertyListCreateData(NULL, result, kCFPropertyListBinaryFormat_v1_0, 0, NULL) : NULL;
    int good = encoded && CFDataGetLength(encoded) <= 8*1024*1024+4096 && original_directory();
    size_t offset = 0;
    while (good && offset < (size_t)CFDataGetLength(encoded)) {
        ssize_t count = pwrite(descriptor, CFDataGetBytePtr(encoded)+offset,
            (size_t)CFDataGetLength(encoded)-offset, (off_t)offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { good = 0; break; }
        offset += (size_t)count;
    }
    if (good) good = fsync(descriptor) == 0;
    if (encoded) CFRelease(encoded);
    if (result) CFRelease(result);
    CFRelease(code); CFRelease(stdout_data); CFRelease(stderr_data);
    return good;
}

int main(int argc, char **argv) {
    if (argc != 13) return 64;
    int fds[12];
    for (unsigned int index = 0; index < 12; ++index) {
        fds[index] = parse_fd(argv[index+1], 0);
        if (fds[index] < 0 || fcntl(fds[index], F_SETFD, FD_CLOEXEC)) return 64;
        for (unsigned int earlier = 0; earlier < index; ++earlier) if (fds[index] == fds[earlier]) return 64;
    }
    producer_directory_fd = fds[9]; device_directory_fd = fds[10];
    signal(SIGCHLD, SIG_DFL); signal(SIGPIPE, SIG_IGN);
    if (!start_liveness(fds[4])) return 64;
    CFDataRef raw = read_private(fds[0], 64*1024), command_raw = read_private(fds[5], 128*1024);
    if (!raw || !command_raw) owner_exit(64);
    CFPropertyListRef config = CFPropertyListCreateWithData(NULL, raw, kCFPropertyListImmutable, NULL, NULL);
    CFPropertyListRef command = CFPropertyListCreateWithData(NULL, command_raw, kCFPropertyListImmutable, NULL, NULL);
    if (!config || CFGetTypeID(config) != CFDictionaryGetTypeID() ||
        (CFDictionaryGetCount(config) != 18 && CFDictionaryGetCount(config) != 25) ||
        !command || CFGetTypeID(command) != CFArrayGetTypeID() || CFArrayGetCount(command) < 4 || CFArrayGetCount(command) > 128) owner_exit(64);
    const CFStringRef keys[] = {CFSTR("schemaVersion"), CFSTR("operationId"), CFSTR("requestDigest"), CFSTR("contextDigest"),
        CFSTR("scopeDigest"), CFSTR("definitionDigest"), CFSTR("workPath"), CFSTR("commandDigest"),
        CFSTR("childWorkPath"), CFSTR("maxOutputBytes"), CFSTR("nativeBindingDigest"),
        CFSTR("ownershipGeneration"), CFSTR("hostIncarnation"), CFSTR("helperIncarnation"),
        CFSTR("adbPath"), CFSTR("adbSha256"), CFSTR("sandboxSha256"), CFSTR("stdinDigest")};
    for (unsigned int index = 0; index < 18; ++index) if (!CFDictionaryContainsKey(config, keys[index])) owner_exit(64);
    if (CFDictionaryGetCount(config) == 25) {
        const CFStringRef extra[] = {CFSTR("toolKind"), CFSTR("packageInspectorPath"), CFSTR("packageInspectorSha256"),
            CFSTR("packageInspectorSupport"), CFSTR("apkPath"), CFSTR("apkSha256"), CFSTR("apkBytes")};
        for (unsigned index = 0; index < 7; ++index) if (!CFDictionaryContainsKey(config, extra[index])) owner_exit(64);
    }
    char version[8], digest[65], child_work[PATH_MAX], canonical[PATH_MAX], maximum_text[16];
    if (!field_string(config, keys[0], version, sizeof(version)) || strcmp(version, "1") ||
        !field_string(config, keys[7], digest, sizeof(digest)) || !hex_digest(digest) || !same_digest(command_raw, digest) ||
        !field_string(config, keys[8], child_work, sizeof(child_work)) || !realpath(child_work, canonical) || strcmp(child_work, canonical) ||
        !field_string(config, keys[9], maximum_text, sizeof(maximum_text))) owner_exit(64);
    char *end = NULL; errno = 0; unsigned long maximum = strtoul(maximum_text, &end, 10);
    if (errno || !end || *end || maximum < 1 || maximum > 4*1024*1024 || !start_owner(config, fds)) owner_exit(64);
    char operation_path[PATH_MAX], expected_child[PATH_MAX];
    if (fcntl(producer_directory_fd, F_GETPATH, operation_path)) owner_exit(64);
    snprintf(expected_child, sizeof(expected_child), "%s/staging", operation_path);
    if (strcmp(child_work, expected_child) ||
        !owned_named(fds[6], "native-result.plist", 1)) owner_exit(64);
    char **arguments = calloc((size_t)CFArrayGetCount(command)+1, sizeof(char *));
    if (!arguments) owner_exit(64);
    for (CFIndex index = 0; index < CFArrayGetCount(command); ++index) {
        CFStringRef value = CFArrayGetValueAtIndex(command, index);
        if (!value || CFGetTypeID(value) != CFStringGetTypeID() || CFStringGetLength(value) > 32768) owner_exit(64);
        CFIndex length = CFStringGetMaximumSizeForEncoding(CFStringGetLength(value), kCFStringEncodingUTF8)+1;
        arguments[index] = malloc((size_t)length);
        if (!arguments[index] || !CFStringGetCString(value, arguments[index], length, kCFStringEncodingUTF8)) owner_exit(64);
        for (CFIndex letter = 0; letter < CFStringGetLength(value); ++letter)
            if (!CFStringGetCharacterAtIndex(value, letter)) owner_exit(64);
    }
    if (!fixed_command(config, command, arguments, fds[11])) owner_exit(64);
    int out_pipe[2], error_pipe[2];
    if (pipe(out_pipe) || pipe(error_pipe)) owner_exit(64);
    unsigned char *out = malloc(maximum), *error = malloc(maximum);
    if (!out || !error) owner_exit(64);
    char temporary[PATH_MAX+16]; snprintf(temporary, sizeof(temporary), "TMPDIR=%s", child_work);
    char *environment[] = {"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C", temporary, NULL};
    signal(SIGTERM, interrupted_signal); signal(SIGINT, interrupted_signal);
    pthread_mutex_lock(&owner_child_lock);
    pid_t child = fork();
    if (child == 0) {
        /* The verification child also retains both locks if its guardian is
         * abruptly killed. The pinned tool's FD retention is acceptance-tested. */
        fcntl(fds[2], F_SETFD, 0); fcntl(fds[3], F_SETFD, 0);
        close(out_pipe[0]); close(error_pipe[0]);
        if (dup2(fds[11], STDIN_FILENO) < 0 || dup2(out_pipe[1], STDOUT_FILENO) < 0 || dup2(error_pipe[1], STDERR_FILENO) < 0 || chdir(child_work)) _exit(127);
        close(out_pipe[1]); close(error_pipe[1]);
        execve(arguments[0], arguments, environment); _exit(127);
    }
    owner_child_pid = child > 0 ? child : 0;
    pthread_mutex_unlock(&owner_child_lock);
    if (child < 0) owner_exit(64);
    close(out_pipe[1]); close(error_pipe[1]);
    fcntl(out_pipe[0], F_SETFL, O_NONBLOCK); fcntl(error_pipe[0], F_SETFL, O_NONBLOCK);
    size_t sizes[2] = {0, 0}; int ended[2] = {0, 0}, finished = 0, status = 0, bounded = 1;
    int readers[2] = {out_pipe[0], error_pipe[0]}; unsigned char *buffers[2] = {out, error};
    while (!finished || !ended[0] || !ended[1]) {
        if (interrupted) owner_exit(78);
        if (!finished) finished = owner_poll_child(&status);
        struct pollfd pollers[2] = {{readers[0], POLLIN|POLLHUP, 0}, {readers[1], POLLIN|POLLHUP, 0}};
        poll(pollers, 2, 10);
        for (unsigned int index = 0; index < 2; ++index) {
            if (ended[index]) continue;
            unsigned char chunk[8192]; ssize_t count = read(readers[index], chunk, sizeof(chunk));
            if (count == 0) ended[index] = 1;
            else if (count > 0) {
                size_t available = maximum-sizes[index];
                size_t take = (size_t)count < available ? (size_t)count : available;
                memcpy(buffers[index]+sizes[index], chunk, take); sizes[index] += take;
                if ((size_t)count > take) { bounded = 0; owner_stop_child(); finished = 1; status = SIGKILL; }
            } else if (errno != EAGAIN && errno != EINTR) owner_exit(74);
        }
    }
    close(readers[0]); close(readers[1]);
    int return_code = WIFEXITED(status) ? WEXITSTATUS(status) : WIFSIGNALED(status) ? -WTERMSIG(status) : 127;
    if (!save_result(fds[6], return_code, out, sizes[0], error, sizes[1], bounded)) owner_exit(74);
    free(out); free(error);
    for (CFIndex index = 0; index < CFArrayGetCount(command); ++index) free(arguments[index]);
    free(arguments); CFRelease(command); CFRelease(config); CFRelease(raw); CFRelease(command_raw);
    finish_owner(return_code == 0 && bounded ? 0 : 1);
    return 74;
}
