/* Fixed, material-free validation through the public macOS Security API.
 * The caller stages and bounds the app, pins this binary, and confines it to
 * that staging directory. Only trustd's local evaluation endpoint is needed.
 * Security's macOS static verifier disables certificate Keychain searches;
 * kSecCSNoNetworkAccess also disables network access during validation.
 * Source review: Apple Security db15acbe6a7f257a859ad9a3bb86097bfe0679d9,
 * OSX/libsecurity_codesigning/lib/StaticCode.cpp, verifySignature().
 */
#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>
#include <CommonCrypto/CommonDigest.h>
#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int parse_integer(const char *text, int32_t *value) {
    if (!text[0] || strlen(text) > 11 || (text[0] != '-' && (text[0] < '0' || text[0] > '9'))) return 0;
    char *end = NULL;
    errno = 0;
    long number = strtol(text, &end, 10);
    if (errno || end == text || *end || number < INT32_MIN || number > INT32_MAX) return 0;
    *value = (int32_t)number;
    return 1;
}

static OSStatus write_metadata(SecStaticCodeRef code, CFNumberRef cpu, CFNumberRef subtype) {
    CFDictionaryRef information = NULL, entitlements = NULL, document = NULL;
    CFDataRef leaf_der = NULL, output = NULL, entitlement_plist = NULL;
    CFStringRef checksum = NULL;
    CFNumberRef version = NULL;
    OSStatus status = SecCodeCopySigningInformation((SecCodeRef)code, kSecCSSigningInformation, &information);
    if (status) goto done;
    if (!information) { status = errSecCSInvalidObjectRef; goto done; }
    status = errSecCSInvalidObjectRef;
    CFStringRef identifier = CFDictionaryGetValue(information, kSecCodeInfoIdentifier);
    CFStringRef team = CFDictionaryGetValue(information, kSecCodeInfoTeamIdentifier);
    CFNumberRef flags = CFDictionaryGetValue(information, kSecCodeInfoFlags);
    CFArrayRef certificates = CFDictionaryGetValue(information, kSecCodeInfoCertificates);
    int64_t signature_flags = 0;
    if (!identifier || CFGetTypeID(identifier) != CFStringGetTypeID() || CFStringGetLength(identifier) > 1024 ||
        !team || CFGetTypeID(team) != CFStringGetTypeID() || CFStringGetLength(team) > 64 ||
        !flags || CFGetTypeID(flags) != CFNumberGetTypeID() ||
        !CFNumberGetValue(flags, kCFNumberSInt64Type, &signature_flags) ||
        (signature_flags & kSecCodeSignatureAdhoc) || !certificates ||
        CFGetTypeID(certificates) != CFArrayGetTypeID() || CFArrayGetCount(certificates) < 1 ||
        CFArrayGetCount(certificates) > 8) goto done;
    for (CFIndex index = 0; index < CFArrayGetCount(certificates); ++index) {
        SecCertificateRef certificate = (SecCertificateRef)CFArrayGetValueAtIndex(certificates, index);
        if (!certificate || CFGetTypeID(certificate) != SecCertificateGetTypeID()) goto done;
        CFDataRef der = SecCertificateCopyData(certificate);
        if (!der) goto done;
        if (CFDataGetLength(der) <= 0 || CFDataGetLength(der) > 128 * 1024) { CFRelease(der); goto done; }
        if (index == 0) leaf_der = der; else CFRelease(der);
    }
    unsigned char hash[CC_SHA256_DIGEST_LENGTH];
    char hex[65];
    CC_SHA256(CFDataGetBytePtr(leaf_der), (CC_LONG)CFDataGetLength(leaf_der), hash);
    for (size_t index = 0; index < sizeof(hash); ++index) snprintf(hex + index * 2, 3, "%02x", hash[index]);
    checksum = CFStringCreateWithCString(NULL, hex, kCFStringEncodingASCII);
    /* The dictionary view can add compatibility aliases. Parse the verified
     * entitlement blob, which Security emits before applying those aliases. */
    CFDataRef entitlement_blob = CFDictionaryGetValue(information, kSecCodeInfoEntitlements);
    if (entitlement_blob) {
        if (CFGetTypeID(entitlement_blob) != CFDataGetTypeID() || CFDataGetLength(entitlement_blob) <= 8 ||
            CFDataGetLength(entitlement_blob) > 256 * 1024 + 8) goto done;
        uint32_t header[2];
        memcpy(header, CFDataGetBytePtr(entitlement_blob), sizeof(header));
        if (ntohl(header[0]) != 0xfade7171 || ntohl(header[1]) != CFDataGetLength(entitlement_blob)) goto done;
        entitlement_plist = CFDataCreate(NULL, CFDataGetBytePtr(entitlement_blob) + 8,
            CFDataGetLength(entitlement_blob) - 8);
        if (!entitlement_plist) goto done;
        entitlements = (CFDictionaryRef)CFPropertyListCreateWithData(NULL, entitlement_plist,
            kCFPropertyListImmutable, NULL, NULL);
    } else {
        if (CFDictionaryContainsKey(information, kSecCodeInfoEntitlementsDict)) goto done;
        entitlements = CFDictionaryCreate(NULL, NULL, NULL, 0,
            &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    }
    if (!entitlements || CFGetTypeID(entitlements) != CFDictionaryGetTypeID() ||
        CFDictionaryGetCount(entitlements) > 128) goto done;
    int one = 1;
    version = CFNumberCreate(NULL, kCFNumberIntType, &one);
    if (!checksum || !version) goto done;
    const void *keys[] = {CFSTR("schemaVersion"), CFSTR("cpuType"), CFSTR("cpuSubtype"),
        CFSTR("bundleId"), CFSTR("teamId"), CFSTR("certificateSha256"), CFSTR("entitlements")};
    const void *values[] = {version, cpu, subtype, identifier, team, checksum, entitlements};
    document = CFDictionaryCreate(NULL, keys, values, 7,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    if (!document) goto done;
    output = CFPropertyListCreateData(NULL, document, kCFPropertyListBinaryFormat_v1_0, 0, NULL);
    if (!output || CFDataGetLength(output) <= 0 || CFDataGetLength(output) > 1024 * 1024) goto done;
    CFIndex offset = 0;
    while (offset < CFDataGetLength(output)) {
        ssize_t written = write(STDOUT_FILENO, CFDataGetBytePtr(output) + offset,
            (size_t)(CFDataGetLength(output) - offset));
        if (written < 0 && errno == EINTR) continue;
        if (written <= 0) goto done;
        offset += written;
    }
    status = errSecSuccess;
done:
    if (output) CFRelease(output);
    if (document) CFRelease(document);
    if (entitlements) CFRelease(entitlements);
    if (entitlement_plist) CFRelease(entitlement_plist);
    if (version) CFRelease(version);
    if (checksum) CFRelease(checksum);
    if (leaf_der) CFRelease(leaf_der);
    if (information) CFRelease(information);
    return status;
}

int main(int argc, char **argv) {
    if ((argc != 2 && argc != 4) || argv[1][0] != '/' || strlen(argv[1]) >= PATH_MAX) return 64;
    CFDictionaryRef attributes = NULL;
    CFNumberRef cpu = NULL, subtype = NULL;
    if (argc == 4) {
        int32_t cpu_value, subtype_value;
        if (!parse_integer(argv[2], &cpu_value) || !parse_integer(argv[3], &subtype_value)) return 64;
        cpu = CFNumberCreate(NULL, kCFNumberSInt32Type, &cpu_value);
        subtype = CFNumberCreate(NULL, kCFNumberSInt32Type, &subtype_value);
        if (!cpu || !subtype) { if (cpu) CFRelease(cpu); if (subtype) CFRelease(subtype); return 1; }
        const void *keys[] = {kSecCodeAttributeArchitecture, kSecCodeAttributeSubarchitecture};
        const void *values[] = {cpu, subtype};
        attributes = CFDictionaryCreate(NULL, keys, values, 2,
            &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    }
    CFURLRef url = CFURLCreateFromFileSystemRepresentation(NULL,
        (const UInt8 *)argv[1], (CFIndex)strlen(argv[1]), true);
    SecStaticCodeRef code = NULL;
    OSStatus status = errSecAllocate;
    if (!url || (argc == 4 && !attributes)) goto done;
    status = SecStaticCodeCreateWithPathAndAttributes(url, kSecCSDefaultFlags, attributes, &code);
    if (status == errSecSuccess && code) {
        status = SecStaticCodeCheckValidity(code,
            kSecCSCheckAllArchitectures | kSecCSCheckNestedCode | kSecCSStrictValidate |
            kSecCSNoNetworkAccess | kSecCSConsiderExpiration, NULL);
    } else if (status == errSecSuccess) {
        status = errSecInternalComponent;
    }
    if (status == errSecSuccess && argc == 4) status = write_metadata(code, cpu, subtype);
done:
    if (argc == 2) printf("{\"schemaVersion\":1,\"ok\":%s,\"nativeStatus\":%d}\n",
        status == errSecSuccess ? "true" : "false", (int)status);
    if (code) CFRelease(code);
    if (url) CFRelease(url);
    if (attributes) CFRelease(attributes);
    if (cpu) CFRelease(cpu);
    if (subtype) CFRelease(subtype);
    return status == errSecSuccess ? 0 : 1;
}
