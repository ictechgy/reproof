
#include <arpa/inet.h>
#include <fcntl.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
static int opened(const char *p, int flags) {
    int fd=open(p,flags,0600); if(fd<0) return 0; close(fd); return 1;
}
static int connected(const char *kind,const char *value) {
    int fd=socket(!strcmp(kind,"unix")?AF_UNIX:AF_INET,SOCK_STREAM,0),ok=0;
    if(fd<0) return 0;
    if(!strcmp(kind,"unix")) {
        struct sockaddr_un a={0}; a.sun_family=AF_UNIX;
        if(strlen(value)<sizeof(a.sun_path)) {
            strcpy(a.sun_path,value); ok=connect(fd,(struct sockaddr*)&a,sizeof(a))==0;
        }
    } else {
        struct sockaddr_in a={0}; a.sin_family=AF_INET;
        a.sin_port=htons((unsigned short)atoi(value));
        inet_pton(AF_INET,"127.0.0.1",&a.sin_addr);
        ok=connect(fd,(struct sockaddr*)&a,sizeof(a))==0;
    }
    close(fd); return ok;
}
#define STAGE(name) do { \
    static const char message[] = "REPRO_PREFLIGHT_STAGE " name "\n"; \
    (void)write(STDERR_FILENO,message,sizeof(message)-1); \
} while (0)
int main(int argc,char **argv) {
    STAGE("main-entered");
    if(argc!=9) return 64;
    STAGE("allowed-read"); int ar=opened(argv[1],O_RDONLY);
    STAGE("allowed-write"); int aw=opened(argv[2],O_WRONLY|O_CREAT|O_EXCL);
    STAGE("outside-read"); int dr=opened(argv[3],O_RDONLY);
    STAGE("outside-write"); int dw=opened(argv[4],O_WRONLY|O_CREAT|O_EXCL);
    STAGE("allowed-network"); int an=connected(argv[5],argv[6]);
    STAGE("outside-network"); int dn=connected("tcp",argv[7]);
    STAGE("unlisted-exec");
    pid_t child=0; char *args[]={"/usr/bin/true",NULL};
    int spawned=posix_spawn(&child,args[0],NULL,NULL,args,environ)==0;
    if(spawned) waitpid(child,NULL,0);
    STAGE("result");
    printf("{\"allowedRead\":%d,\"allowedWrite\":%d,\"allowedNetwork\":%d,"
           "\"outsideRead\":%d,\"outsideWrite\":%d,\"outsideNetwork\":%d,\"unlistedExec\":%d}\n",
           ar,aw,an,dr,dw,dn,spawned);
    return strcmp(argv[8],"reproof-sandbox-probe-v1")?64:0;
}
