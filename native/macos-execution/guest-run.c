/* Fixed guest launcher: resource limits and identity changes before exec.
 * Avoid Python preexec_fn in the agent's multithreaded protocol process. */
#include "guest-guard.h"
#include <sys/resource.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int number(const char *text, long low, long high, long *out) {
    char *end = NULL;
    *out = strtol(text, &end, 10);
    return text[0] != 0 && end != NULL && *end == 0 && *out >= low && *out <= high;
}
static int limit(int kind, rlim_t value) {
    struct rlimit limits = {value, value};
    return setrlimit(kind, &limits) == 0;
}

int main(int argc, char **argv) {
    if (!repro_guest_scope()) { puts("guest-scope-required"); return 2; }
    if (argc == 2 && strcmp(argv[1], "--preflight") == 0) { puts("guest-scope-present"); return 0; }
    long uid, gid, cpu;
    if (argc < 5 || argc > 68 || !number(argv[1], 501, 60000, &uid)
        || !number(argv[2], 1, 60000, &gid) || !number(argv[3], 1, 86401, &cpu)
        || argv[4][0] != '/') { puts("guest-job-rejected"); return 2; }
    for (int index = 4; index < argc; ++index) {
        if (strlen(argv[index]) > 4096) { puts("guest-job-rejected"); return 2; }
    }
    if (!limit(RLIMIT_CORE, 0) || !limit(RLIMIT_NOFILE, 256) || !limit(RLIMIT_NPROC, 128)
        || !limit(RLIMIT_FSIZE, 64 * 1024 * 1024) || !limit(RLIMIT_CPU, (rlim_t)cpu)
        || setgroups(0, NULL) != 0 || setgid((gid_t)gid) != 0 || setuid((uid_t)uid) != 0) {
        puts("guest-job-rejected"); return 2;
    }
    umask(0077);
    execv(argv[4], &argv[4]);
    puts("guest-job-unavailable");
    return 2;
}
