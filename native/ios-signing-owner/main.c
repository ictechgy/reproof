/* Fixed local iOS signing. The SPI declarations are verified against Apple
 * Security db15acbe6a7f257a859ad9a3bb86097bfe0679d9, SecCodeSignerRemote.h.
 * No keychain import, candidate hook, external signer or timestamp service.
 */
#include "ownership.h"
#include <Security/Security.h>
#include <CommonCrypto/CommonDigest.h>
#include <arpa/inet.h>

typedef struct __SecCodeSignerRemote *SecCodeSignerRemoteRef;
typedef CFDataRef (^SecCodeRemoteSignHandler)(CFDataRef, SecCSDigestAlgorithm, SecKeyAlgorithm);
extern OSStatus SecCodeSignerRemoteCreate(CFDictionaryRef, CFArrayRef, SecCSFlags,
                                          SecCodeSignerRemoteRef *, CFErrorRef *);
extern OSStatus SecCodeSignerRemoteAddSignature(SecCodeSignerRemoteRef, SecStaticCodeRef,
                                                SecCSFlags, SecCodeRemoteSignHandler, CFErrorRef *);
extern const CFStringRef kSecCodeSignerIdentifier, kSecCodeSignerTeamIdentifier;
extern const CFStringRef kSecCodeSignerRequireTimestamp, kSecCodeSignerSigningTime;
extern const CFStringRef kSecCodeSignerEntitlements, kSecCodeSignerDigestAlgorithm, kSecCodeSignerFlags;

static int same_digest(CFDataRef data, const char *expected) {
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    char hex[65];
    CC_SHA256(CFDataGetBytePtr(data), (CC_LONG)CFDataGetLength(data), hash);
    for (size_t i = 0; i < sizeof(hash); ++i) snprintf(hex + 2*i, 3, "%02x", hash[i]);
    return strcmp(hex, expected) == 0;
}

static CFDataRef read_password(int descriptor) {
    struct stat info;
    if (fstat(descriptor, &info)) return NULL;
    if (S_ISREG(info.st_mode)) return read_private(descriptor, 512);
    if (!S_ISFIFO(info.st_mode) || info.st_uid != getuid() ||
        (fcntl(descriptor, F_GETFL) & O_ACCMODE) != O_RDONLY) return NULL;
    unsigned char bytes[513] = {0};
    size_t length = 0;
    while (length < sizeof(bytes)) {
        ssize_t count = read(descriptor, bytes+length, sizeof(bytes)-length);
        if (count < 0 && errno == EINTR) continue;
        if (count < 0) { length = 0; break; }
        if (!count) break;
        length += (size_t)count;
    }
    CFDataRef data = length && length <= 512 ? CFDataCreate(NULL, bytes, (CFIndex)length) : NULL;
    volatile unsigned char *clear = bytes;
    for (size_t index = 0; index < sizeof(bytes); ++index) clear[index] = 0;
    return data;
}

static int identifier(const char *text, int team) {
    size_t length = strlen(text);
    if (!length || length > (team ? 64 : 1024)) return 0;
    for (const char *p = text; *p; ++p) {
        if ((*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9')) continue;
        if (!team && ((*p >= 'a' && *p <= 'z') || *p == '.' || *p == '-')) continue;
        return 0;
    }
    return team || strchr(text, '.') != NULL;
}

static int relative_bundle(const char *text) {
    if (!strcmp(text, ".")) return 1;
    if (!text[0] || text[0] == '/' || strlen(text) >= PATH_MAX/2) return 0;
    const char *part = text;
    for (const char *p = text; ; ++p) {
        if (*p && ((unsigned char)*p < 0x20 || *p == '\\' || *p == 0x7f)) return 0;
        if (*p == '/' || !*p) {
            size_t size = (size_t)(p - part);
            if (!size || (size == 1 && part[0] == '.') || (size == 2 && part[0] == '.' && part[1] == '.')) return 0;
            if (!*p) return 1;
            part = p+1;
        }
    }
}

static CFDataRef entitlement_blob(CFDataRef plist) {
    if (!plist || CFGetTypeID(plist) != CFDataGetTypeID() || CFDataGetLength(plist) <= 0 ||
        CFDataGetLength(plist) > 256*1024) return NULL;
    CFPropertyListRef dictionary = CFPropertyListCreateWithData(NULL, plist, kCFPropertyListImmutable, NULL, NULL);
    if (!dictionary) return NULL;
    int valid = CFGetTypeID(dictionary) == CFDictionaryGetTypeID() && CFDictionaryGetCount(dictionary) <= 128;
    CFRelease(dictionary);
    if (!valid) return NULL;
    CFMutableDataRef data = CFDataCreateMutable(NULL, CFDataGetLength(plist)+8);
    if (!data) return NULL;
    uint32_t header[] = {htonl(0xfade7171), htonl((uint32_t)CFDataGetLength(plist)+8)};
    CFDataAppendBytes(data, (const UInt8 *)header, sizeof(header));
    CFDataAppendBytes(data, CFDataGetBytePtr(plist), CFDataGetLength(plist));
    return data;
}

static int sign_bundle(CFDictionaryRef row, CFArrayRef chain, CFStringRef team, SecKeyRef key) {
    const CFStringRef keys[] = {CFSTR("bundlePath"), CFSTR("bundleId"), CFSTR("entitlements")};
    if (!row || CFGetTypeID(row) != CFDictionaryGetTypeID() || CFDictionaryGetCount(row) != 3) return 0;
    for (unsigned int i = 0; i < 3; ++i) if (!CFDictionaryContainsKey(row, keys[i])) return 0;
    char relative[PATH_MAX/2], bundle_id[1025], requested[PATH_MAX], resolved[PATH_MAX], app[PATH_MAX];
    if (!field_string(row, keys[0], relative, sizeof(relative)) || !relative_bundle(relative) ||
        !field_string(row, keys[1], bundle_id, sizeof(bundle_id)) || !identifier(bundle_id, 0) || !original_directory()) return 0;
    int length = snprintf(app, sizeof(app), "%s/App.app", owner.work_path);
    if (length <= 0 || (size_t)length >= sizeof(app)) return 0;
    length = snprintf(requested, sizeof(requested), "%s%s%s", app, !strcmp(relative, ".") ? "" : "/",
        !strcmp(relative, ".") ? "" : relative);
    if (length <= 0 || (size_t)length >= sizeof(requested) || !realpath(requested, resolved) ||
        strncmp(resolved, app, strlen(app)) || (resolved[strlen(app)] && resolved[strlen(app)] != '/')) return 0;
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL, (const UInt8 *)requested, strlen(requested), true);
    CFBundleRef bundle = url ? CFBundleCreate(NULL, url) : NULL;
    CFStringRef actual_id = bundle ? CFBundleGetIdentifier(bundle) : NULL;
    CFDictionaryRef info = bundle ? CFBundleGetInfoDictionary(bundle) : NULL;
    CFStringRef expected_id = CFDictionaryGetValue(row, keys[1]);
    CFDataRef entitlements = entitlement_blob(CFDictionaryGetValue(row, keys[2]));
    SecCodeSignerRemoteRef signer = NULL;
    SecStaticCodeRef code = NULL;
    CFDictionaryRef parameters = NULL;
    CFNumberRef algorithm = NULL, flags = NULL;
    int result = 0;
    atomic_uint callback_count = 0;
    atomic_uint *counter = &callback_count;
    if (!actual_id || !CFEqual(actual_id, expected_id) || !entitlements || !info ||
        CFDictionaryContainsKey(info, CFSTR("CFBundleResourceSpecification"))) goto done;
    int hash = kSecCodeSignatureHashSHA256, zero = 0;
    algorithm = CFNumberCreate(NULL, kCFNumberIntType, &hash);
    flags = CFNumberCreate(NULL, kCFNumberIntType, &zero);
    if (!algorithm || !flags) goto done;
    const void *parameter_keys[] = {kSecCodeSignerIdentifier, kSecCodeSignerTeamIdentifier,
        kSecCodeSignerRequireTimestamp, kSecCodeSignerSigningTime, kSecCodeSignerEntitlements,
        kSecCodeSignerDigestAlgorithm, kSecCodeSignerFlags};
    const void *values[] = {expected_id, team, kCFBooleanFalse, kCFNull, entitlements, algorithm, flags};
    parameters = CFDictionaryCreate(NULL, parameter_keys, values, 7,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (!parameters || SecCodeSignerRemoteCreate(parameters, chain, (1<<7)|(1<<9), &signer, NULL) || !signer ||
        SecStaticCodeCreateWithPath(url, kSecCSDefaultFlags, &code) || !code) goto done;
    OSStatus status = SecCodeSignerRemoteAddSignature(signer, code, kSecCSDefaultFlags,
        ^CFDataRef(CFDataRef digest, SecCSDigestAlgorithm digest_algorithm, SecKeyAlgorithm signature_algorithm) {
            if (atomic_fetch_add(counter, 1) >= 32 || !digest || CFDataGetLength(digest) != 32 ||
                digest_algorithm != kSecCodeSignatureHashSHA256 || !signature_algorithm ||
                !CFEqual(signature_algorithm, kSecKeyAlgorithmRSASignatureDigestPKCS1v15SHA256)) return NULL;
            return SecKeyCreateSignature(key, kSecKeyAlgorithmRSASignatureDigestPKCS1v15SHA256, digest, NULL);
        }, NULL);
    result = status == errSecSuccess && atomic_load(counter) > 0;
done:
    if (signer) CFRelease(signer);
    if (code) CFRelease(code);
    if (parameters) CFRelease(parameters);
    if (algorithm) CFRelease(algorithm);
    if (flags) CFRelease(flags);
    if (entitlements) CFRelease(entitlements);
    if (bundle) CFRelease(bundle);
    if (url) CFRelease(url);
    return result;
}

static unsigned int sign_app(CFDictionaryRef config, int p12_fd, int password_fd, int *succeeded) {
    CFDataRef p12 = read_private(p12_fd, 8*1024*1024), password_bytes = read_password(password_fd);
    CFStringRef password = NULL;
    CFDictionaryRef options = NULL;
    CFArrayRef items = NULL;
    SecCertificateRef certificate = NULL;
    SecKeyRef key = NULL;
    CFDataRef certificate_der = NULL;
    CFMutableArrayRef chain = NULL;
    CFDictionaryRef attributes = NULL;
    unsigned int signed_objects = 0;
    char expected_digest[65], team_id[65];
    if (!p12 || !password_bytes || !field_string(config, CFSTR("certificateSha256"), expected_digest, sizeof(expected_digest)) ||
        !hex_digest(expected_digest) || !field_string(config, CFSTR("teamId"), team_id, sizeof(team_id)) || !identifier(team_id, 1)) goto done;
    CFArrayRef encoded_chain = CFDictionaryGetValue(config, CFSTR("certificateChain"));
    CFArrayRef objects = CFDictionaryGetValue(config, CFSTR("codeObjects"));
    if (!encoded_chain || CFGetTypeID(encoded_chain) != CFArrayGetTypeID() || CFArrayGetCount(encoded_chain) < 1 ||
        CFArrayGetCount(encoded_chain) > 8 || !objects || CFGetTypeID(objects) != CFArrayGetTypeID() ||
        CFArrayGetCount(objects) < 1 || CFArrayGetCount(objects) > 512) goto done;
    chain = CFArrayCreateMutable(NULL, CFArrayGetCount(encoded_chain), &kCFTypeArrayCallBacks);
    if (!chain) goto done;
    for (CFIndex i = 0; i < CFArrayGetCount(encoded_chain); ++i) {
        CFDataRef der = CFArrayGetValueAtIndex(encoded_chain, i);
        if (!der || CFGetTypeID(der) != CFDataGetTypeID() || CFDataGetLength(der) <= 0 ||
            CFDataGetLength(der) > 128*1024 || (i == 0 && !same_digest(der, expected_digest))) goto done;
        SecCertificateRef cert = SecCertificateCreateWithData(NULL, der);
        if (!cert) goto done;
        int duplicate = CFArrayContainsValue(chain, CFRangeMake(0, CFArrayGetCount(chain)), cert);
        if (!duplicate) CFArrayAppendValue(chain, cert);
        CFRelease(cert);
        if (duplicate) goto done;
    }
    password = CFStringCreateWithBytes(NULL, CFDataGetBytePtr(password_bytes), CFDataGetLength(password_bytes),
        kCFStringEncodingUTF8, false);
    if (!password) goto done;
    const void *keys[] = {kSecImportExportPassphrase, kSecImportToMemoryOnly};
    const void *values[] = {password, kCFBooleanTrue};
    options = CFDictionaryCreate(NULL, keys, values, 2, &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (!options || SecPKCS12Import(p12, options, &items) || !items || CFArrayGetCount(items) != 1) goto done;
    CFDictionaryRef item = CFArrayGetValueAtIndex(items, 0);
    if (!item || CFGetTypeID(item) != CFDictionaryGetTypeID()) goto done;
    SecIdentityRef identity = (SecIdentityRef)CFDictionaryGetValue(item, kSecImportItemIdentity);
    if (!identity || CFGetTypeID(identity) != SecIdentityGetTypeID() ||
        SecIdentityCopyCertificate(identity, &certificate) || !certificate) goto done;
    certificate_der = SecCertificateCopyData(certificate);
    if (!certificate_der || !same_digest(certificate_der, expected_digest) ||
        SecIdentityCopyPrivateKey(identity, &key) || !key || !SecKeyIsAlgorithmSupported(key,
        kSecKeyOperationTypeSign, kSecKeyAlgorithmRSASignatureDigestPKCS1v15SHA256)) goto done;
    attributes = SecKeyCopyAttributes(key);
    CFNumberRef key_size = attributes ? CFDictionaryGetValue(attributes, kSecAttrKeySizeInBits) : NULL;
    int bits = 0;
    if (!key_size || CFGetTypeID(key_size) != CFNumberGetTypeID() ||
        !CFNumberGetValue(key_size, kCFNumberIntType, &bits) || bits < 2048 || bits > 8192) goto done;
    for (CFIndex i = 0; i < CFArrayGetCount(objects); ++i) {
        if (!sign_bundle(CFArrayGetValueAtIndex(objects, i), chain, CFDictionaryGetValue(config, CFSTR("teamId")), key)) {
            goto done;
        }
        ++signed_objects;
    }
    *succeeded = 1;
done:
    if (attributes) CFRelease(attributes);
    if (key) CFRelease(key);
    if (certificate_der) CFRelease(certificate_der);
    if (certificate) CFRelease(certificate);
    if (items) CFRelease(items);
    if (options) CFRelease(options);
    if (password) CFRelease(password);
    if (chain) CFRelease(chain);
    if (p12) CFRelease(p12);
    if (password_bytes) CFRelease(password_bytes);
    return signed_objects;
}

int main(int argc, char **argv) {
    if (argc != 10) return 64;
    int descriptors[9];
    for (unsigned int i = 0; i < 9; ++i) {
        descriptors[i] = parse_fd(argv[i+1], i == 5 || i == 6);
        if (descriptors[i] < 0) return 64;
        for (unsigned int j = 0; j < i; ++j)
            if (descriptors[i] && descriptors[i] == descriptors[j]) return 64;
        if (descriptors[i] && fcntl(descriptors[i], F_SETFD, FD_CLOEXEC)) return 64;
    }
    if (!start_liveness(descriptors[4])) return 64;
    CFDataRef bytes = read_private(descriptors[0], 2*1024*1024);
    if (!bytes) return 64;
    CFPropertyListRef decoded = CFPropertyListCreateWithData(NULL, bytes, kCFPropertyListImmutable, NULL, NULL);
    CFRelease(bytes);
    if (!decoded || CFGetTypeID(decoded) != CFDictionaryGetTypeID()) { if (decoded) CFRelease(decoded); return 64; }
    CFDictionaryRef config = decoded;
    const CFStringRef keys[] = {CFSTR("schemaVersion"), CFSTR("mode"), CFSTR("operationId"), CFSTR("requestDigest"),
        CFSTR("contextDigest"), CFSTR("scopeDigest"), CFSTR("definitionDigest"), CFSTR("workPath"),
        CFSTR("appRelativePath"), CFSTR("certificateSha256"), CFSTR("teamId"), CFSTR("certificateChain"), CFSTR("codeObjects")};
    if (CFDictionaryGetCount(config) != 13) { CFRelease(decoded); return 64; }
    for (unsigned int i = 0; i < 13; ++i) if (!CFDictionaryContainsKey(config, keys[i])) { CFRelease(decoded); return 64; }
    char version[8], mode[32], app[16];
    /* CFPropertyList can canonicalize integral real values into integers.
     * This private native protocol uses an exact string version instead. */
    if (!field_string(config, keys[0], version, sizeof(version)) || strcmp(version, "1") ||
        !field_string(config, keys[1], mode, sizeof(mode)) ||
        (strcmp(mode, "sign") && strcmp(mode, "liveness-probe")) || !field_string(config, keys[8], app, sizeof(app)) ||
        strcmp(app, "App.app") || (!strcmp(mode, "sign") ? descriptors[5] == 0 || descriptors[6] == 0 : descriptors[5] != 0 || descriptors[6] != 0) ||
        !start_owner(config, descriptors)) { CFRelease(decoded); return 64; }
    if (!strcmp(mode, "liveness-probe")) for (;;) pause();
    int succeeded = 0;
    unsigned int count = sign_app(config, descriptors[5], descriptors[6], &succeeded);
    CFRelease(decoded);
    close(descriptors[5]); close(descriptors[6]);
    finish_owner(succeeded ? 0 : 1, count);
    return 74;
}
