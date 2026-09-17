/* Installed as root in an owned guest. No caller-selected target or program. */
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/vsock.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "guest-guard.h"

int main(int argc, char **argv) {
    int preflight = argc == 2 && strcmp(argv[1], "--preflight") == 0;
    if ((argc != 1 && !preflight) || !repro_guest_scope()) {
        puts("guest-scope-required");
        return 2;
    }
    if (preflight) { puts("guest-scope-present"); return 0; }
    int channel = socket(AF_VSOCK, SOCK_STREAM, 0);
    if (channel < 0) { puts("guest-channel-unavailable"); return 2; }
    struct sockaddr_vm address;
    memset(&address, 0, sizeof(address));
    address.svm_len = sizeof(address);
    address.svm_family = AF_VSOCK;
    address.svm_cid = VMADDR_CID_HOST;
    address.svm_port = 4050;
    if (connect(channel, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(channel);
        puts("guest-channel-unavailable");
        return 2;
    }
    char descriptor[24];
    snprintf(descriptor, sizeof(descriptor), "%d", channel);
    char *arguments[] = {"python3", "-I", "/Library/ReproLoopGuest/main.py", "--channel-fd", descriptor, NULL};
    char *environment[] = {"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "LANG=C", NULL};
    execve("/Library/ReproLoopGuest/python/bin/python3", arguments, environment);
    close(channel);
    puts("guest-agent-unavailable");
    return 2;
}
