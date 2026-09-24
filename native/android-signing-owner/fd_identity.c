#include <jni.h>

#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

JNIEXPORT jboolean JNICALL
Java_io_reproof_signing_SigningOwner_validateFdPathNative(
        JNIEnv *environment, jclass owner_class, jint descriptor,
        jstring path_value, jboolean directory, jboolean zero_size,
        jboolean require_lock) {
    (void)owner_class;
    if (descriptor < 3 || path_value == NULL) return JNI_FALSE;
    const char *path = (*environment)->GetStringUTFChars(environment, path_value, NULL);
    if (path == NULL) return JNI_FALSE;
    int named_descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat opened = {0};
    struct stat named = {0};
    bool valid = named_descriptor >= 0
            && fstat(descriptor, &opened) == 0
            && fstat(named_descriptor, &named) == 0
            && opened.st_dev == named.st_dev
            && opened.st_ino == named.st_ino
            && opened.st_uid == getuid()
            && named.st_uid == getuid()
            && (directory ? S_ISDIR(opened.st_mode) : S_ISREG(opened.st_mode))
            && (directory || (opened.st_nlink == 1 && named.st_nlink == 1))
            && ((opened.st_mode & 0777) == (directory ? 0700 : 0600))
            && ((named.st_mode & 0777) == (directory ? 0700 : 0600))
            && (!zero_size || opened.st_size == 0)
            && (!require_lock || flock(descriptor, LOCK_EX | LOCK_NB) == 0);
    if (named_descriptor >= 0) close(named_descriptor);
    (*environment)->ReleaseStringUTFChars(environment, path_value, path);
    return valid ? JNI_TRUE : JNI_FALSE;
}

JNIEXPORT jboolean JNICALL
Java_io_reproof_signing_SigningOwner_validatePrivateRegularFdNative(
        JNIEnv *environment, jclass owner_class, jint descriptor) {
    (void)environment;
    (void)owner_class;
    struct stat info = {0};
    bool valid = descriptor >= 3
            && fstat(descriptor, &info) == 0
            && S_ISREG(info.st_mode)
            && info.st_uid == getuid()
            && info.st_nlink == 1
            && (info.st_mode & 0777) == 0600
            && info.st_size > 0;
    return valid ? JNI_TRUE : JNI_FALSE;
}
