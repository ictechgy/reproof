#ifndef REPRO_GUEST_GUARD_H
#define REPRO_GUEST_GUARD_H
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <unistd.h>

static int repro_guest_scope(void) {
    int present = 0;
    size_t length = sizeof(present);
    struct stat info;
    return geteuid() == 0 && getuid() == 0
        && sysctlbyname("kern.hv_vmm_present", &present, &length, NULL, 0) == 0
        && present == 1
        && lstat("/Library/ReproLoopGuest", &info) == 0
        && S_ISDIR(info.st_mode) && info.st_uid == 0 && (info.st_mode & 0022) == 0;
}
#endif
