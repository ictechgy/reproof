/* Fixed selected-device queries and gated install commands. Retains original locks and reaps the direct
 * child on parent-pipe EOF. This does not confine or stop CoreDevice services. */
#include <CommonCrypto/CommonDigest.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach/mach_time.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t child_lock = PTHREAD_MUTEX_INITIALIZER;
static pid_t child_pid;
static int live_fd;
static int completion_fd=-1;
static int completion_reported;
static uint64_t native_deadline;
static volatile sig_atomic_t stopping;

static void request_stop(int number) { (void)number;stopping=1; }

static uint64_t continuous_ns(void) {
    mach_timebase_info_data_t base;
    if (mach_timebase_info(&base) || !base.numer || !base.denom) return UINT64_MAX;
    return (uint64_t)(((__uint128_t)mach_continuous_time()*base.numer)/base.denom);
}
static int expired(void) {
    return native_deadline && continuous_ns()>=native_deadline;
}
static int owned_pipe(int fd, int mode) {
    struct stat info;
    return !fstat(fd,&info) && S_ISFIFO(info.st_mode) && info.st_uid==getuid() &&
        (fcntl(fd,F_GETFL)&O_ACCMODE)==mode;
}

static void completion_write(char value) {
    if (completion_fd<0 || completion_reported) return;
    ssize_t written;
    do { written=write(completion_fd,&value,1); } while (written<0 && errno==EINTR);
    if (written==1) completion_reported=1;
}
static void completion_fallback(void) {
    /* No SDK child was created, so releasing the guardian is safe.  Once a
     * child exists, only collect_child() may publish the completion byte. */
    if (child_pid<=0) completion_write('D');
}

static int descriptor(const char *text) {
    if (!*text || strlen(text)>7) return -1;
    for (const char *p=text; *p; ++p) if (*p<'0' || *p>'9') return -1;
    long value=strtol(text,NULL,10);
    return value>=3 && value<=1000000 ? (int)value : -1;
}
static int hex(const char *text, size_t size) {
    if (strlen(text)!=size) return 0;
    for (const char *p=text; *p; ++p)
        if (!(*p>='0' && *p<='9') && !(*p>='a' && *p<='f')) return 0;
    return 1;
}
static int private_directory(int fd) {
    struct stat info;
    return !fstat(fd,&info) && S_ISDIR(info.st_mode) && info.st_uid==getuid() &&
        (info.st_mode&0777)==0700;
}
static int original_lock(int fd, int directory, const char *name, int empty) {
    struct stat opened,named;
    if (!private_directory(directory) || fstat(fd,&opened) || !S_ISREG(opened.st_mode) ||
        opened.st_uid!=getuid() || opened.st_nlink!=1 || (opened.st_mode&0777)!=0600 ||
        opened.st_size<0 || opened.st_size>64 || (empty && opened.st_size) ||
        (fcntl(fd,F_GETFL)&O_ACCMODE)!=O_RDWR ||
        fstatat(directory,name,&named,AT_SYMLINK_NOFOLLOW) || !S_ISREG(named.st_mode) ||
        named.st_dev!=opened.st_dev || named.st_ino!=opened.st_ino) return 0;
    int probe=openat(directory,name,O_RDWR|O_NOFOLLOW|O_NONBLOCK);
    if (probe<0) return 0;
    int result=flock(probe,LOCK_EX|LOCK_NB);
    int held=result!=0 && (errno==EWOULDBLOCK || errno==EAGAIN);
    close(probe);
    return held && !flock(fd,LOCK_EX|LOCK_NB);
}
static int pinned_file(const char *path, const char *expected, int tool) {
    char canonical[PATH_MAX];
    if (path[0]!='/' || !realpath(path,canonical) || strcmp(path,canonical) || !hex(expected,64)) return 0;
    int fd=open(path,O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if (fd<0) return 0;
    struct stat before,after; unsigned char magic[4];
    int good=!fstat(fd,&before) && S_ISREG(before.st_mode) && before.st_nlink==1 &&
        (before.st_uid==0 || before.st_uid==getuid()) && !(before.st_mode&0022) &&
        (!tool || (before.st_mode&0111)) && before.st_size>4 &&
        before.st_size<=(tool ? 64*1024*1024 : 128*1024) && pread(fd,magic,4,0)==4;
    const unsigned char formats[][4]={{0xcf,0xfa,0xed,0xfe},{0xfe,0xed,0xfa,0xcf},
        {0xca,0xfe,0xba,0xbe},{0xbe,0xba,0xfe,0xca},{0xca,0xfe,0xba,0xbf},{0xbf,0xba,0xfe,0xca}};
    int macho=0;
    for (unsigned i=0;good && i<sizeof(formats)/sizeof(formats[0]);++i)
        if (!memcmp(magic,formats[i],4)) macho=1;
    good=good && (!tool || macho);
    CC_SHA256_CTX context; CC_SHA256_Init(&context);
    unsigned char bytes[16384],hash[CC_SHA256_DIGEST_LENGTH]; size_t total=0;
    while (good) {
        ssize_t count=read(fd,bytes,sizeof(bytes));
        if (count<0 && errno==EINTR) continue;
        if (count<0) { good=0; break; }
        if (!count) break;
        total+=(size_t)count;
        if (total>(size_t)before.st_size) { good=0; break; }
        CC_SHA256_Update(&context,bytes,(CC_LONG)count);
    }
    good=good && total==(size_t)before.st_size && !fstat(fd,&after) &&
        before.st_size==after.st_size && before.st_mtimespec.tv_sec==after.st_mtimespec.tv_sec &&
        before.st_mtimespec.tv_nsec==after.st_mtimespec.tv_nsec &&
        before.st_ctimespec.tv_sec==after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec==after.st_ctimespec.tv_nsec;
    close(fd); CC_SHA256_Final(hash,&context);
    char digest[65];
    for (size_t i=0;i<sizeof(hash);++i) snprintf(digest+i*2,3,"%02x",hash[i]);
    return good && !strcmp(digest,expected);
}
static int pinned_tool(const char *path, const char *expected) { return pinned_file(path,expected,1); }
static int snapshot_configuration(int directory, const char *expected) {
    int input=openat(directory,"session.xctestrun",O_RDONLY|O_NOFOLLOW|O_NONBLOCK);
    if (input<0) return -1;
    struct stat before,after;
    unsigned char bytes[128*1024+1],hash[CC_SHA256_DIGEST_LENGTH];size_t count=0;
    int good=!fstat(input,&before) && S_ISREG(before.st_mode) && before.st_uid==getuid() &&
        before.st_nlink==1 && before.st_size>0 && before.st_size<=128*1024 && !(before.st_mode&0022);
    while (good && count<sizeof(bytes)) {
        ssize_t size=read(input,bytes+count,sizeof(bytes)-count);
        if (size<0 && errno==EINTR) continue;
        if (size<0) { good=0;break; }
        if (!size) break;
        count+=(size_t)size;
    }
    good=good && count==(size_t)before.st_size && !fstat(input,&after) &&
        before.st_size==after.st_size && before.st_mtimespec.tv_sec==after.st_mtimespec.tv_sec &&
        before.st_mtimespec.tv_nsec==after.st_mtimespec.tv_nsec &&
        before.st_ctimespec.tv_sec==after.st_ctimespec.tv_sec && before.st_ctimespec.tv_nsec==after.st_ctimespec.tv_nsec;
    close(input);if (!good) return -1;
    CC_SHA256(bytes,(CC_LONG)count,hash);char digest[65];
    for (size_t i=0;i<sizeof(hash);++i) snprintf(digest+i*2,3,"%02x",hash[i]);
    if (strcmp(digest,expected)) return -1;
    int output=openat(directory,".native-session.xctestrun",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
    if (output<0) return -1;
    size_t offset=0;
    while (offset<count) {
        ssize_t size=write(output,bytes+offset,count-offset);
        if (size<0 && errno==EINTR) continue;
        if (size<=0) { good=0;break; }
        offset+=(size_t)size;
    }
    if (fsync(output)) good=0;
    if (fchmod(output,0400)) good=0;
    int selected=good ? openat(directory,".native-session.xctestrun",O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC) : -1;
    struct stat written,opened;
    good=good && selected>=0 && !fstat(output,&written) && !fstat(selected,&opened) &&
        S_ISREG(opened.st_mode) && opened.st_nlink==1 && opened.st_uid==getuid() &&
        opened.st_dev==written.st_dev && opened.st_ino==written.st_ino && opened.st_size==(off_t)count;
    close(output);
    if (!good && selected>=0) { close(selected);selected=-1; }
    return selected;
}
/* The direct child remains waitable until the last signal to its process
 * group. Never signal a numeric PID/group after reaping that anchor. */
static int collect_child(int *status) {
    pid_t group=child_pid;
    if (group<=0) return 1;
    /* Darwin may return EPERM for a group containing only a waitable zombie.
     * It is still necessary to reap and observe the entire group disappear. */
    if (kill(-group,SIGKILL) && errno!=ESRCH && errno!=EPERM) return 0;
    pid_t waited;
    do { waited=waitpid(group,status,0); } while (waited<0 && errno==EINTR);
    if (waited!=group) return 0;
    child_pid=0;
    while (!kill(-group,0) || errno==EPERM) {
        struct timespec delay={.tv_sec=0,.tv_nsec=10000000};nanosleep(&delay,NULL);
    }
    return errno==ESRCH;
}
static void *watch_parent(void *unused) {
    (void)unused;
    struct pollfd input={.fd=live_fd,.events=POLLIN|POLLHUP};
    int result;
    uint64_t ceiling=continuous_ns()+900000000000ULL;
    do {
        result=poll(&input,1,10);
    } while (!stopping && !expired() && continuous_ns()<ceiling &&
        ((result<0 && errno==EINTR) || result==0));
    /* No messages are accepted. EOF, data, errors, and the fixed ceiling all
     * revoke the child. The original PID is reaped under one mutex. */
    pthread_mutex_lock(&child_lock);
    if (child_pid>0) {
        int status;
        if (!collect_child(&status)) for (;;) pause();
        completion_write('D');
    } else completion_write('D');
    _exit(result==0 ? 77 : 75);
}
int main(int argc, char **argv) {
    if (argc!=13 && argc!=14 && argc!=16 && argc!=17 && argc!=18 && argc!=20 && argc!=21 && argc!=22) return 64;
    int mutation=argc>=16, xctest=argc==20 || argc==21 || argc==22;
    int recovery=argc==18 || argc==22;
    int with_completion=(argc==14 || argc==17 || argc==18 || argc==21 || argc==22);
    if (with_completion) {
        completion_fd=descriptor(argv[argc-1]);
        if (completion_fd<0 || fcntl(completion_fd,F_SETFD,FD_CLOEXEC) || !owned_pipe(completion_fd,O_WRONLY)) return 64;
        atexit(completion_fallback);
    }
    int fds[7];
    for (unsigned i=0;i<(mutation ? 7u : 5u);++i) {
        fds[i]=descriptor(argv[i<5 ? i+1 : i+8]);
        if (fds[i]<0 || fcntl(fds[i],F_SETFD,FD_CLOEXEC)) return 64;
        for (unsigned j=0;j<i;++j) if (fds[j]==fds[i]) return 64;
        if (with_completion && fds[i]==completion_fd) return 64;
    }
    if (!hex(argv[6],64)) return 64;
    char lock_name[80]; snprintf(lock_name,sizeof(lock_name),"%s.lock",argv[6]);
    if (!original_lock(fds[0],fds[1],"producer.lock",1) ||
        !original_lock(fds[2],fds[3],lock_name,0) || !pinned_tool(argv[7],argv[8])) return 64;
    int app_root=fds[1];
    if (recovery) {
        app_root=descriptor(argv[xctest ? 20 : 16]);
        if (app_root<0 || app_root==completion_fd || !private_directory(app_root) ||
            fcntl(app_root,F_SETFD,FD_CLOEXEC)) return 64;
        for (unsigned i=0;i<7;++i) if (app_root==fds[i]) return 64;
        char origin[PATH_MAX],selected[PATH_MAX],prefix[PATH_MAX];
        if (fcntl(fds[1],F_GETPATH,origin) || fcntl(app_root,F_GETPATH,selected) ||
            snprintf(prefix,sizeof(prefix),"%s/native-recovery/attempt-",origin)>=(int)sizeof(prefix)) return 64;
        size_t size=strlen(prefix);
        if (strncmp(selected,prefix,size) || strlen(selected+size)!=3 || selected[size]!='0' ||
            selected[size+1]!='0' || selected[size+2]<'1' || selected[size+2]>'3') return 64;
        int parent=openat(fds[1],"native-recovery",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if (parent<0 || !private_directory(parent)) { if (parent>=0) close(parent);return 64; }
        char leaf[16];snprintf(leaf,sizeof(leaf),"attempt-%s",selected+size);
        int nested=openat(parent,leaf,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);close(parent);
        struct stat left,right;
        int same=nested>=0 && !fstat(nested,&left) && !fstat(app_root,&right) &&
            left.st_dev==right.st_dev && left.st_ino==right.st_ino;
        if (nested>=0) close(nested);
        if (!same || (xctest ? strcmp(argv[9],"xctest-original") : strcmp(argv[9],"restore-original"))) return 64;
    }
    if (mutation) {
        if (xctest) {
            if (strcmp(argv[9],"xctest-candidate") && strcmp(argv[9],"xctest-original")) return 64;
        } else if (strcmp(argv[9],"install-candidate") && strcmp(argv[9],"restore-original")) return 64;
        const char *value=argv[15];
        if (!*value || strlen(value)>19) return 64;
        for (const char *p=value;*p;++p) if (*p<'0' || *p>'9') return 64;
        errno=0;native_deadline=strtoull(value,NULL,10);
        if (errno || !native_deadline || native_deadline>INT64_MAX || expired() ||
            !owned_pipe(fds[5],O_WRONLY) || !owned_pipe(fds[6],O_RDONLY)) return 64;
    } else if (strcmp(argv[9],"details") && strcmp(argv[9],"apps") && strcmp(argv[9],"processes") &&
        strcmp(argv[9],"runtime-identity")) return 64;
    if (strlen(argv[10])!=36) return 64;
    for (unsigned i=0;i<36;++i) {
        char c=argv[10][i];
        if (i==8 || i==13 || i==18 || i==23) { if (c!='-') return 64; }
        else if (!(c>='0' && c<='9') && !(c>='a' && c<='f') && !(c>='A' && c<='F')) return 64;
    }
    if (!*argv[11] || strlen(argv[11])>180 || !strchr(argv[11],'.')) return 64;
    for (const char *p=argv[11];*p;++p)
        if (!(*p>='a' && *p<='z') && !(*p>='A' && *p<='Z') && !(*p>='0' && *p<='9') && *p!='.' && *p!='-') return 64;
    char work[PATH_MAX],output[PATH_MAX],temporary[PATH_MAX+16];
    if (argv[12][0]!='/' || !realpath(argv[12],work) || strcmp(work,argv[12]) ||
        snprintf(output,sizeof(output),"%s/result.json",work)>=(int)sizeof(output)) return 64;
    int directory=open(work,O_RDONLY|O_DIRECTORY|O_NOFOLLOW);
    if (directory<0 || !private_directory(directory)) return 64;
    struct stat info,work_identity,home_identity;
    if (fstat(directory,&work_identity) || fcntl(directory,F_SETFD,FD_CLOEXEC)) return 64;
    if (!fstatat(directory,"result.json",&info,AT_SYMLINK_NOFOLLOW) || errno!=ENOENT) return 64;
    if (!owned_pipe(fds[4],O_RDONLY)) return 64;
    char app[PATH_MAX],configuration[PATH_MAX],result_bundle[PATH_MAX],destination[256],identity_output[PATH_MAX];
    char developer[PATH_MAX+32],home[PATH_MAX+32];
    int config_fd=-1;
    char config_argument[PATH_MAX];
    int runtime_identity=!strcmp(argv[9],"runtime-identity");
    if (runtime_identity && (snprintf(identity_output,sizeof(identity_output),"%s/identity.json",work)>=(int)sizeof(identity_output) ||
        !fstatat(directory,"identity.json",&info,AT_SYMLINK_NOFOLLOW) || errno!=ENOENT)) return 64;
    if (xctest) {
        char operation[PATH_MAX],expected[PATH_MAX],canonical[PATH_MAX];
        if (strlen(argv[18])!=1 || argv[18][0]<'1' || argv[18][0]>'3' || !hex(argv[19],64) ||
            fcntl(app_root,F_GETPATH,operation) ||
            snprintf(expected,sizeof(expected),"%s/command-%s-%03d-work",operation,argv[9],argv[18][0]-'0')>=(int)sizeof(expected) ||
            strcmp(work,expected) || argv[16][0]!='/' || !realpath(argv[16],canonical) || strcmp(argv[16],canonical) ||
            snprintf(expected,sizeof(expected),"%s/usr/bin/xcodebuild",argv[16])>=(int)sizeof(expected) ||
            strcmp(argv[7],expected)) return 64;
        if (!*argv[17] || strlen(argv[17])>128) return 64;
        for (const char *p=argv[17];*p;++p)
            if (!(*p>='a' && *p<='z') && !(*p>='A' && *p<='Z') && !(*p>='0' && *p<='9') && *p!='-') return 64;
        if (snprintf(configuration,sizeof(configuration),"%s/session.xctestrun",work)>=(int)sizeof(configuration) ||
            snprintf(result_bundle,sizeof(result_bundle),"%s/result.xcresult",work)>=(int)sizeof(result_bundle) ||
            snprintf(destination,sizeof(destination),"platform=iOS,id=%s",argv[17])>=(int)sizeof(destination) ||
            !pinned_file(configuration,argv[19],0) ||
            !fstatat(directory,"result.xcresult",&info,AT_SYMLINK_NOFOLLOW) || errno!=ENOENT) return 64;
        int user_home=openat(directory,"home",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if (user_home<0 || !private_directory(user_home) || fstat(user_home,&home_identity)) return 64;
        close(user_home);
        config_fd=snapshot_configuration(directory,argv[19]);if (config_fd<0) return 64;
        if (snprintf(config_argument,sizeof(config_argument),"%s/.native-session.xctestrun",work)>=(int)sizeof(config_argument)) return 64;
        snprintf(developer,sizeof(developer),"DEVELOPER_DIR=%s",argv[16]);
        snprintf(home,sizeof(home),"HOME=%s/home",work);
    } else if (mutation) {
        char operation[PATH_MAX],expected[PATH_MAX],canonical[PATH_MAX];
        const char *role=!strcmp(argv[9],"install-candidate") ? "candidate" : "original";
        if (fcntl(app_root,F_GETPATH,operation) ||
            snprintf(expected,sizeof(expected),"%s/command-%s-work",operation,argv[9])>=(int)sizeof(expected) ||
            strcmp(work,expected) ||
            snprintf(app,sizeof(app),"%s/%s/App.app",operation,role)>=(int)sizeof(app) ||
            !realpath(app,canonical) || strcmp(app,canonical)) return 64;
        int selected=openat(app_root,role,O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        if (selected<0 || !private_directory(selected)) return 64;
        int bundle=openat(selected,"App.app",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);
        close(selected);
        if (bundle<0 || !private_directory(bundle)) return 64;
        close(bundle);
    }
    live_fd=fds[4]; umask(0077);
    struct rlimit core={0,0}; if (setrlimit(RLIMIT_CORE,&core)) return 64;
    signal(SIGCHLD,SIG_DFL);signal(SIGPIPE,SIG_IGN);
    signal(SIGTERM,request_stop);signal(SIGINT,request_stop);
    char *command[20]={argv[7],"device","info",argv[9],"--device",argv[10],NULL};
    unsigned index=6;
    if (xctest) {
        index=1;command[index++]="test-without-building";command[index++]="-xctestrun";
        command[index++]=config_argument;command[index++]="-destination";command[index++]=destination;
        command[index++]="-resultBundlePath";command[index++]=result_bundle;
        command[index++]="-parallel-testing-enabled";command[index++]="NO";
        command[index++]="-only-testing:ReproLiveTests/LiveControlTests/testControlSession";
    } else if (mutation) { command[2]="install";command[3]="app";command[index++]=app; }
    else if (runtime_identity) {
        command[2]="copy";command[3]="from";
        command[index++]="--domain-type";command[index++]="appDataContainer";
        command[index++]="--domain-identifier";command[index++]=argv[11];
        command[index++]="--source";command[index++]="Library/Application Support/ReproLoop/runtime-identity.json";
        command[index++]="--destination";command[index++]=identity_output;
    }
    else if (!strcmp(argv[9],"apps")) { command[index++]="--bundle-id";command[index++]=argv[11]; }
    if (!xctest) { command[index++]="--json-output";command[index++]=output; }
    command[index]=NULL;
    snprintf(temporary,sizeof(temporary),"TMPDIR=%s",work);
    char *environment[]={"PATH=/usr/bin:/bin","LANG=C","LC_ALL=C",temporary,NULL,NULL,NULL};
    if (xctest) { environment[4]=developer;environment[5]=home; }
    pthread_t watcher;
    if (pthread_create(&watcher,NULL,watch_parent,NULL) || pthread_detach(watcher)) return 64;
    if (mutation) {
        if (expired() || write(fds[5],"R",1)!=1) return 75;
        struct pollfd gate={.fd=fds[6],.events=POLLIN|POLLHUP};
        int ready;
        do { ready=poll(&gate,1,10); } while (!expired() &&
            (ready==0 || (ready<0 && errno==EINTR)));
        char grant[2];
        if (expired() || ready<=0 || read(fds[6],grant,sizeof(grant))!=1 || grant[0]!='G' ||
            !pinned_tool(argv[7],argv[8]) || (xctest && (!pinned_file(configuration,argv[19],0) ||
                !pinned_file(config_argument,argv[19],0)))) return 75;
        if (lstat(work,&info) || !S_ISDIR(info.st_mode) || info.st_dev!=work_identity.st_dev ||
            info.st_ino!=work_identity.st_ino || !fstatat(directory,"result.json",&info,AT_SYMLINK_NOFOLLOW) || errno!=ENOENT) return 75;
        if (xctest && (fstatat(directory,"home",&info,AT_SYMLINK_NOFOLLOW) || !S_ISDIR(info.st_mode) ||
            info.st_dev!=home_identity.st_dev || info.st_ino!=home_identity.st_ino ||
            !fstatat(directory,"result.xcresult",&info,AT_SYMLINK_NOFOLLOW) || errno!=ENOENT)) return 75;
    }
    pthread_mutex_lock(&child_lock);
    struct pollfd parent={.fd=live_fd,.events=POLLIN|POLLHUP};
    if (stopping || expired() || poll(&parent,1,0)!=0) { pthread_mutex_unlock(&child_lock);return 75; }
    pid_t child=fork();
    if (child==0) {
        rlim_t maximum=xctest ? 64*1024*1024 : 256*1024;
        struct rlimit output_limit={maximum,maximum};
        if (setpgid(0,0) || expired() || setrlimit(RLIMIT_FSIZE,&output_limit) || fcntl(fds[0],F_SETFD,0) ||
            fcntl(fds[2],F_SETFD,0) || (config_fd>=0 && fcntl(config_fd,F_SETFD,0)) || fchdir(directory)) _exit(127);
        execve(argv[7],command,environment);_exit(127);
    }
    if (child>0 && setpgid(child,child) && getpgid(child)!=child) {
        kill(child,SIGKILL);int status;while (waitpid(child,&status,0)<0 && errno==EINTR) {}
        pthread_mutex_unlock(&child_lock);return 70;
    }
    child_pid=child>0 ? child : 0;
    pthread_mutex_unlock(&child_lock);
    if (child<0) return 70;
    for (;;) {
        int status=0;
        pthread_mutex_lock(&child_lock);
        siginfo_t observed;memset(&observed,0,sizeof(observed));
        int inspected=waitid(P_PID,(id_t)child_pid,&observed,WEXITED|WNOHANG|WNOWAIT);
        if (!inspected && observed.si_pid==child_pid) {
            if (!collect_child(&status)) for (;;) pause();
            completion_write('D');
            pthread_mutex_unlock(&child_lock);
            return WIFEXITED(status) ? WEXITSTATUS(status) : WIFSIGNALED(status) ? 128+WTERMSIG(status) : 71;
        }
        pthread_mutex_unlock(&child_lock);
        struct timespec delay={.tv_sec=0,.tv_nsec=10000000};nanosleep(&delay,NULL);
    }
}
