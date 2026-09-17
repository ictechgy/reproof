/* Private process guardian for the fixed iOS verification adapters.
 * The command is constructed by trusted adapter code, never issue/AI input.
 * Only a sandbox-exec command can be launched. No signing material is passed.
 */
#define REPRO_OWNER_CHILDREN 1
#include "../ios-signing-owner/ownership.h"
#include <CommonCrypto/CommonDigest.h>

static volatile sig_atomic_t interrupted;
static void interrupted_signal(int number) { (void)number; interrupted = 1; }

static int same_digest(CFDataRef body, const char *expected) {
    unsigned char hash[CC_SHA256_DIGEST_LENGTH]; char hex[65];
    CC_SHA256(CFDataGetBytePtr(body), (CC_LONG)CFDataGetLength(body), hash);
    for (size_t index = 0; index < sizeof(hash); ++index) snprintf(hex+2*index, 3, "%02x", hash[index]);
    return strcmp(hex, expected) == 0;
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
    int good = encoded && CFDataGetLength(encoded) <= 2*1024*1024+4096 && original_directory();
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
    if (argc != 10) return 64;
    int fds[9];
    for (unsigned int index = 0; index < 9; ++index) {
        fds[index] = parse_fd(argv[index+1], 0);
        if (fds[index] < 0 || fcntl(fds[index], F_SETFD, FD_CLOEXEC)) return 64;
        for (unsigned int earlier = 0; earlier < index; ++earlier) if (fds[index] == fds[earlier]) return 64;
    }
    if (!start_liveness(fds[4])) return 64;
    CFDataRef raw = read_private(fds[0], 64*1024), command_raw = read_private(fds[5], 128*1024);
    if (!raw || !command_raw) owner_exit(64);
    CFPropertyListRef config = CFPropertyListCreateWithData(NULL, raw, kCFPropertyListImmutable, NULL, NULL);
    CFPropertyListRef command = CFPropertyListCreateWithData(NULL, command_raw, kCFPropertyListImmutable, NULL, NULL);
    if (!config || CFGetTypeID(config) != CFDictionaryGetTypeID() || CFDictionaryGetCount(config) != 10 ||
        !command || CFGetTypeID(command) != CFArrayGetTypeID() || CFArrayGetCount(command) < 4 || CFArrayGetCount(command) > 128) owner_exit(64);
    const CFStringRef keys[] = {CFSTR("schemaVersion"), CFSTR("operationId"), CFSTR("requestDigest"), CFSTR("contextDigest"),
        CFSTR("scopeDigest"), CFSTR("definitionDigest"), CFSTR("workPath"), CFSTR("commandDigest"),
        CFSTR("childWorkPath"), CFSTR("maxOutputBytes")};
    for (unsigned int index = 0; index < 10; ++index) if (!CFDictionaryContainsKey(config, keys[index])) owner_exit(64);
    char version[8], digest[65], child_work[PATH_MAX], canonical[PATH_MAX], maximum_text[16];
    if (!field_string(config, keys[0], version, sizeof(version)) || strcmp(version, "1") ||
        !field_string(config, keys[7], digest, sizeof(digest)) || !hex_digest(digest) || !same_digest(command_raw, digest) ||
        !field_string(config, keys[8], child_work, sizeof(child_work)) || !realpath(child_work, canonical) || strcmp(child_work, canonical) ||
        !field_string(config, keys[9], maximum_text, sizeof(maximum_text))) owner_exit(64);
    char *end = NULL; errno = 0; unsigned long maximum = strtoul(maximum_text, &end, 10);
    if (errno || !end || *end || maximum < 1 || maximum > 1024*1024 || !start_owner(config, fds)) owner_exit(64);
    size_t root_size = strlen(owner.work_path);
    if (strncmp(child_work, owner.work_path, root_size) || (child_work[root_size] && child_work[root_size] != '/') ||
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
    if (strcmp(arguments[0], "/usr/bin/sandbox-exec") || strcmp(arguments[1], "-p")) owner_exit(64);
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
        if (dup2(out_pipe[1], STDOUT_FILENO) < 0 || dup2(error_pipe[1], STDERR_FILENO) < 0 || chdir(child_work)) _exit(127);
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
    finish_owner(return_code == 0 && bounded ? 0 : 1, 0);
    return 74;
}
