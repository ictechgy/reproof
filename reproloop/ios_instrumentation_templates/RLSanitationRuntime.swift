#if DEBUG || REPRO_OBSERVATIONS
import Foundation
import CoreFoundation
import CryptoKit
import Security
import Darwin

@_silgen_name("SecTaskCreateFromSelf")
private func RLSecTaskCreateFromSelf(_ allocator: CFAllocator?) -> CFTypeRef?

@_silgen_name("SecTaskCopyValueForEntitlement")
private func RLSecTaskCopyValueForEntitlement(
    _ task: CFTypeRef,
    _ entitlement: CFString,
    _ error: UnsafeMutablePointer<Unmanaged<CFError>?>?
) -> CFTypeRef?

private enum RLSanitationError: Error {
    case rejected
}

private struct RLSanitationPath {
    let root: String
    let components: [String]
}

private struct RLSanitationKeychainSelector {
    let service: String
    let account: String
}

private struct RLSanitationPolicy {
    let paths: [RLSanitationPath]
    let userDefaultsKeys: [String]
    let keychainSelectors: [RLSanitationKeychainSelector]
}

private struct RLSanitationStartupContext {
    let bundleID: String
    let buildID: String
    let profileDigest: String
    let startedAtMs: Int
}

private let rlSanitationDarwinCallback: CFNotificationCallback = {
    _, _, _, _, _ in RLSanitationRuntime.notificationReceived()
}

/// App-owned, debug-only startup sanitation. The selected policy is compiled
/// into the target; process environment data can request only its exact digest.
@objc(RLSanitationRuntime)
public final class RLSanitationRuntime: NSObject, RLSanitationRuntimeBridge {
    private static let policyKind = "ios-app-owned-sanitation"
    private static let receiptKind = "ios-app-sanitation-receipt"
    private static let digestEnvironmentKey = "REPRO_SANITATION_POLICY_DIGEST"
    private static let maxPolicyBytes = 32 * 1024
    private static let maxTreeDepth = 64
    private static let maxTreeEntries = 100_000
    private static let observer = NSObject()
    private static let lock = NSLock()
    private static var requestedDigest: String?
    private static var selectedPolicy: RLSanitationPolicy?
    private static var launchReceipt: [String: Any]?
    private static var startupContext: RLSanitationStartupContext?
    private static var observerName: CFNotificationName?
    private static var notificationScheduled = false

    /// Called synchronously from the Objective-C `+load` method.
    @objc public static func applyBeforeMain() {
        let environment = ProcessInfo.processInfo.environment
        let sanitationKeys = environment.keys.filter { $0.hasPrefix("REPRO_SANITATION_") }
        guard sanitationKeys.allSatisfy({ $0 == digestEnvironmentKey }) else {
            terminate()
        }
        guard let requested = environment[digestEnvironmentKey] else { return }
        guard Thread.isMainThread else { terminate() }
        do {
            try check(validDigest(requested))
            let runID = try selectedRunID(environment)
            let loaded = try loadCompiledPolicy(expectedDigest: requested)
            let startedAtMs = nowMilliseconds()
            let receipt = try sanitize(loaded, digest: requested, runID: runID,
                                       stage: "launch", startedAtMs: startedAtMs)
            requestedDigest = requested
            selectedPolicy = loaded
            launchReceipt = receipt
        } catch {
            terminate()
        }
    }

    /// Returns only the receipt issued by this process lifetime. No file is
    /// read to reconstruct or forge launch success.
    @objc public static func currentLaunchReceipt() -> NSDictionary? {
        guard Thread.isMainThread, let launchReceipt else { return nil }
        return NSDictionary(dictionary: launchReceipt)
    }

    /// Arms cleanup only after the recorder has validated its normal startup
    /// identity and durably written the launch receipt.
    @objc public static func completeValidatedStartup(
        bundleID: String,
        buildID: String,
        profileDigest: String,
        startedAtMs: Int
    ) -> Bool {
        guard Thread.isMainThread else {
            if requestedDigest != nil { terminate() }
            return false
        }
        guard let digest = requestedDigest else { return true }
        do {
            try check(validBundleID(bundleID) && validBuildID(buildID) && validDigest(profileDigest))
            try check(startedAtMs >= 0 && Bundle.main.bundleIdentifier == bundleID)
            let runID = try selectedRunID(ProcessInfo.processInfo.environment)
            try check(launchReceipt?["runId"] as? String == runID)
            try check(launchReceipt?["policyDigest"] as? String == digest)
            try check(startupContext == nil && observerName == nil)
            startupContext = RLSanitationStartupContext(
                bundleID: bundleID,
                buildID: buildID,
                profileDigest: profileDigest,
                startedAtMs: startedAtMs
            )
            let name = CFNotificationName("io.reproloop.sanitize.\(runID)" as CFString)
            observerName = name
            let pointer = Unmanaged.passUnretained(observer).toOpaque()
            CFNotificationCenterAddObserver(
                CFNotificationCenterGetDarwinNotifyCenter(),
                pointer,
                rlSanitationDarwinCallback,
                name.rawValue,
                nil,
                .deliverImmediately
            )
            return true
        } catch {
            terminate()
        }
    }

    fileprivate static func notificationReceived() {
        lock.lock()
        if notificationScheduled {
            lock.unlock()
            return
        }
        notificationScheduled = true
        lock.unlock()
        DispatchQueue.main.async { handleCleanupOnMain() }
    }

    private static func handleCleanupOnMain() {
        guard Thread.isMainThread else { terminate() }
        guard let policy = selectedPolicy,
              let digest = requestedDigest,
              let context = startupContext,
              let name = observerName,
              let launchReceipt,
              let runID = launchReceipt["runId"] as? String else { terminate() }
        let pointer = Unmanaged.passUnretained(observer).toOpaque()
        CFNotificationCenterRemoveObserver(
            CFNotificationCenterGetDarwinNotifyCenter(), pointer, name, nil
        )
        observerName = nil
        do {
            try check(try selectedRunID(ProcessInfo.processInfo.environment) == runID)
            _ = try loadCompiledPolicy(expectedDigest: digest)
            let startedAtMs = nowMilliseconds()
            let receipt = try sanitize(policy, digest: digest, runID: runID,
                                       stage: "cleanup", startedAtMs: startedAtMs)
            let base = applicationSupportDirectory().appendingPathComponent("ReproLoop", isDirectory: true)
            try ReproRuntimeIdentityWriter.write(
                bundleID: context.bundleID,
                buildID: context.buildID,
                runID: runID,
                profileDigest: context.profileDigest,
                startedAtMs: context.startedAtMs,
                sanitation: receipt,
                to: base
            )
        } catch {
            terminate()
        }
        _exit(0)
    }

    private static func loadCompiledPolicy(expectedDigest: String) throws -> RLSanitationPolicy {
        guard let raw = RLSanitationConfig.policyJSON,
              let compiledDigest = RLSanitationConfig.policyDigest,
              compiledDigest == expectedDigest,
              validDigest(compiledDigest),
              let data = raw.data(using: .utf8),
              data.count <= maxPolicyBytes else { throw RLSanitationError.rejected }
        let calculated = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        try check(calculated == compiledDigest)
        let object = try JSONSerialization.jsonObject(with: data)
        guard let document = object as? [String: Any] else { throw RLSanitationError.rejected }
        let policy = try validatePolicy(document)
        guard let infoDigest = Bundle.main.object(forInfoDictionaryKey: "ReproSanitationPolicyDigest") as? String,
              infoDigest == compiledDigest,
              let infoPolicy = Bundle.main.object(forInfoDictionaryKey: "ReproSanitationPolicy") as? [String: Any],
              NSDictionary(dictionary: infoPolicy).isEqual(to: document) else {
            throw RLSanitationError.rejected
        }
        return policy
    }

    private static func validatePolicy(_ document: [String: Any]) throws -> RLSanitationPolicy {
        try check(Set(document.keys) == Set([
            "schemaVersion", "kind", "paths", "userDefaultsKeys", "keychainGenericPasswords"
        ]))
        try check(integer(document["schemaVersion"]) == 1)
        try check(document["kind"] as? String == policyKind)
        guard let rawPaths = document["paths"] as? [[String: Any]], rawPaths.count <= 64,
              let defaults = document["userDefaultsKeys"] as? [String], defaults.count <= 128,
              let rawKeychain = document["keychainGenericPasswords"] as? [[String: Any]],
              rawKeychain.count <= 32,
              !rawPaths.isEmpty || !defaults.isEmpty || !rawKeychain.isEmpty else {
            throw RLSanitationError.rejected
        }

        var paths: [RLSanitationPath] = []
        var foldedPaths: [(String, [String])] = []
        for item in rawPaths {
            try check(Set(item.keys) == Set(["root", "relativePath"]))
            guard let root = item["root"] as? String,
                  ["documents", "application-support", "caches"].contains(root),
                  let relative = item["relativePath"] as? String,
                  relative.utf8.count <= 512,
                  !relative.hasPrefix("/"), !relative.contains("\\") else {
                throw RLSanitationError.rejected
            }
            let components = relative.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
            try check(!components.isEmpty && components.allSatisfy(validPathComponent))
            let folded = components.map { $0.lowercased() }
            try check(!(root == "application-support" && folded.first == "reproloop"))
            for (otherRoot, other) in foldedPaths where root == otherRoot {
                let count = min(folded.count, other.count)
                try check(Array(folded.prefix(count)) != Array(other.prefix(count)))
            }
            foldedPaths.append((root, folded))
            paths.append(RLSanitationPath(root: root, components: components))
        }

        try check(defaults.allSatisfy(validExactString))
        try check(Set(defaults).count == defaults.count)
        var selectors: [RLSanitationKeychainSelector] = []
        var selectorKeys = Set<String>()
        for item in rawKeychain {
            try check(Set(item.keys) == Set(["service", "account"]))
            guard let service = item["service"] as? String,
                  let account = item["account"] as? String,
                  validExactString(service), validExactString(account) else {
                throw RLSanitationError.rejected
            }
            let selectorKey = service + "\u{0}" + account
            try check(selectorKeys.insert(selectorKey).inserted)
            selectors.append(RLSanitationKeychainSelector(service: service, account: account))
        }
        return RLSanitationPolicy(paths: paths, userDefaultsKeys: defaults,
                                  keychainSelectors: selectors)
    }

    private static func sanitize(
        _ policy: RLSanitationPolicy,
        digest: String,
        runID: String,
        stage: String,
        startedAtMs: Int
    ) throws -> [String: Any] {
        try check(Thread.isMainThread && ["launch", "cleanup"].contains(stage))
        let accessGroup = try keychainAccessGroup(required: !policy.keychainSelectors.isEmpty)
        var keychainCounts: [Int] = []
        if let accessGroup {
            for selector in policy.keychainSelectors {
                keychainCounts.append(try keychainMatchCount(selector, accessGroup: accessGroup))
            }
        }
        try check(keychainCounts.allSatisfy { $0 <= 1 })

        var visitedEntries = 0
        for path in policy.paths {
            try removeSelectedPath(path, visited: &visitedEntries)
        }

        let defaults = UserDefaults.standard
        for key in policy.userDefaultsKeys { defaults.removeObject(forKey: key) }
        // `synchronize()` is required at this boundary, but its deprecated
        // Boolean result is not a stable persistence signal on current iOS.
        // The exact selected-key postcondition below remains fail-closed.
        _ = defaults.synchronize()
        try check(policy.userDefaultsKeys.allSatisfy { defaults.object(forKey: $0) == nil })

        if let accessGroup {
            for (index, selector) in policy.keychainSelectors.enumerated() where keychainCounts[index] == 1 {
                let status = SecItemDelete(keychainQuery(selector, accessGroup: accessGroup) as CFDictionary)
                try check(status == errSecSuccess)
            }
            for selector in policy.keychainSelectors {
                try check(try keychainMatchCount(selector, accessGroup: accessGroup) == 0)
            }
        }

        let completedAtMs = nowMilliseconds()
        try check(completedAtMs >= startedAtMs)
        return [
            "schemaVersion": 1,
            "kind": receiptKind,
            "policyDigest": digest,
            "runId": runID,
            "stage": stage,
            "startedAtMs": startedAtMs,
            "completedAtMs": completedAtMs,
            "pathCount": policy.paths.count,
            "userDefaultsKeyCount": policy.userDefaultsKeys.count,
            "keychainItemCount": policy.keychainSelectors.count,
            "status": "complete"
        ]
    }

    private static func keychainAccessGroup(required: Bool) throws -> String? {
        guard required else { return nil }
        guard let task = RLSecTaskCreateFromSelf(nil),
              let value = RLSecTaskCopyValueForEntitlement(
                task, "keychain-access-groups" as CFString, nil
              ),
              let groups = value as? [String],
              groups.count == 1,
              let group = groups.first,
              validExactString(group),
              !group.contains("*") else { throw RLSanitationError.rejected }
        return group
    }

    private static func keychainQuery(
        _ selector: RLSanitationKeychainSelector,
        accessGroup: String
    ) -> [CFString: Any] {
        return [
            kSecClass: kSecClassGenericPassword,
            kSecAttrService: selector.service,
            kSecAttrAccount: selector.account,
            kSecAttrAccessGroup: accessGroup,
            kSecAttrSynchronizable: kCFBooleanFalse as Any
        ]
    }

    private static func keychainMatchCount(
        _ selector: RLSanitationKeychainSelector,
        accessGroup: String
    ) throws -> Int {
        var query = keychainQuery(selector, accessGroup: accessGroup)
        query[kSecReturnAttributes] = kCFBooleanTrue
        query[kSecMatchLimit] = kSecMatchLimitAll
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return 0 }
        try check(status == errSecSuccess)
        let rows: [[CFString: Any]]
        if let array = result as? [[CFString: Any]] {
            rows = array
        } else if let row = result as? [CFString: Any] {
            rows = [row]
        } else {
            throw RLSanitationError.rejected
        }
        try check(rows.count <= 1)
        try check(rows.allSatisfy {
            $0[kSecAttrAccessGroup] as? String == accessGroup
                && $0[kSecAttrService] as? String == selector.service
                && $0[kSecAttrAccount] as? String == selector.account
        })
        return rows.count
    }

    private static func removeSelectedPath(
        _ selected: RLSanitationPath,
        visited: inout Int
    ) throws {
        let root = try rootURL(selected.root)
        let rootFD = root.path.withCString {
            Darwin.open($0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
        }
        if rootFD < 0 {
            if errno == ENOENT { return }
            throw RLSanitationError.rejected
        }
        var parentFD = rootFD
        defer { Darwin.close(parentFD) }
        if selected.components.count > 1 {
            for component in selected.components.dropLast() {
                let next = component.withCString {
                    Darwin.openat(parentFD, $0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
                }
                if next < 0 {
                    if errno == ENOENT { return }
                    throw RLSanitationError.rejected
                }
                Darwin.close(parentFD)
                parentFD = next
            }
        }
        try removeEntry(parentFD: parentFD, name: selected.components.last!,
                        depth: 0, visited: &visited)
        try check(entryIsAbsent(parentFD: parentFD, name: selected.components.last!))
    }

    private static func removeEntry(
        parentFD: Int32,
        name: String,
        depth: Int,
        visited: inout Int
    ) throws {
        try check(depth <= maxTreeDepth)
        var metadata = stat()
        let status = name.withCString {
            fstatat(parentFD, $0, &metadata, AT_SYMLINK_NOFOLLOW)
        }
        if status < 0 {
            if errno == ENOENT { return }
            throw RLSanitationError.rejected
        }
        visited += 1
        try check(visited <= maxTreeEntries)
        if (metadata.st_mode & S_IFMT) == S_IFDIR {
            let childFD = name.withCString {
                Darwin.openat(parentFD, $0, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW)
            }
            try check(childFD >= 0)
            defer { Darwin.close(childFD) }
            let streamFD = Darwin.dup(childFD)
            try check(streamFD >= 0)
            guard let directory = fdopendir(streamFD) else {
                Darwin.close(streamFD)
                throw RLSanitationError.rejected
            }
            defer { closedir(directory) }
            errno = 0
            while let entry = readdir(directory) {
                let childName = withUnsafePointer(to: entry.pointee.d_name) {
                    $0.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
                }
                if childName == "." || childName == ".." { continue }
                try removeEntry(parentFD: childFD, name: childName,
                                depth: depth + 1, visited: &visited)
                errno = 0
            }
            try check(errno == 0)
            let removed = name.withCString { unlinkat(parentFD, $0, AT_REMOVEDIR) }
            try check(removed == 0)
        } else {
            let removed = name.withCString { unlinkat(parentFD, $0, 0) }
            try check(removed == 0)
        }
    }

    private static func entryIsAbsent(parentFD: Int32, name: String) -> Bool {
        var metadata = stat()
        let status = name.withCString {
            fstatat(parentFD, $0, &metadata, AT_SYMLINK_NOFOLLOW)
        }
        return status < 0 && errno == ENOENT
    }

    private static func rootURL(_ root: String) throws -> URL {
        let directory: FileManager.SearchPathDirectory
        switch root {
        case "documents": directory = .documentDirectory
        case "application-support": directory = .applicationSupportDirectory
        case "caches": directory = .cachesDirectory
        default: throw RLSanitationError.rejected
        }
        guard let value = FileManager.default.urls(for: directory, in: .userDomainMask).first else {
            throw RLSanitationError.rejected
        }
        return value
    }

    private static func selectedRunID(_ environment: [String: String]) throws -> String {
        let current = environment["REPRO_RUN_ID"]
        let legacy = environment["REPRO_AUTO_RUN_ID"]
        try check(current == nil || legacy == nil || current == legacy)
        guard let raw = current ?? legacy,
              let uuid = UUID(uuidString: raw),
              uuid.uuidString.lowercased() == raw.lowercased() else {
            throw RLSanitationError.rejected
        }
        return uuid.uuidString.lowercased()
    }

    private static func validPathComponent(_ value: String) -> Bool {
        guard !value.isEmpty, value.utf8.count <= 128,
              let first = value.utf8.first, isASCIIAlphanumeric(first) else { return false }
        return value.utf8.allSatisfy {
            isASCIIAlphanumeric($0) || $0 == 95 || $0 == 45 || $0 == 46
        }
    }

    private static func validExactString(_ value: String) -> Bool {
        let bytes = value.utf8
        return !bytes.isEmpty && bytes.count <= 256 && bytes.allSatisfy { (0x20...0x7e).contains($0) }
    }

    private static func isASCIIAlphanumeric(_ value: UInt8) -> Bool {
        (48...57).contains(value) || (65...90).contains(value) || (97...122).contains(value)
    }

    private static func validDigest(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy {
            (48...57).contains($0) || (97...102).contains($0)
        }
    }

    private static func validBundleID(_ value: String) -> Bool {
        value.count <= 180 && value.range(
            of: "^[A-Za-z][A-Za-z0-9-]*(\\.[A-Za-z][A-Za-z0-9-]*)+$",
            options: .regularExpression
        ) != nil
    }

    private static func validBuildID(_ value: String) -> Bool {
        (8...128).contains(value.utf8.count) && value.utf8.allSatisfy {
            isASCIIAlphanumeric($0) || $0 == 95 || $0 == 45
        }
    }

    private static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue >= 0,
              number.doubleValue.rounded(.towardZero) == number.doubleValue,
              number.doubleValue <= Double(Int.max) else { return nil }
        return number.intValue
    }

    private static func applicationSupportDirectory() -> URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    }

    private static func nowMilliseconds() -> Int {
        Int(Date().timeIntervalSince1970 * 1000.0)
    }

    private static func check(_ condition: @autoclosure () throws -> Bool) throws {
        guard try condition() else { throw RLSanitationError.rejected }
    }

    private static func terminate() -> Never {
        _exit(78)
    }
}
#endif
