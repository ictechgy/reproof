#if DEBUG || REPRO_OBSERVATIONS
import Foundation
import CoreFoundation

enum ReproRuntimeIdentityWriter {
    static let filename = "runtime-identity.json"
    static let kind = "ios-runtime-identity"
    static let maxBytes = 4096

    static func markerData(bundleID: String, buildID: String, runID: String,
                           profileDigest: String, startedAtMs: Int,
                           sanitation: [String: Any]? = nil) throws -> Data {
        guard validBundleID(bundleID), validBuildID(buildID), validUUID(runID),
              validDigest(profileDigest), startedAtMs >= 0 else {
            throw NSError(domain: "ReproRuntimeIdentity", code: 1)
        }
        var object: [String: Any] = [
            "schemaVersion": sanitation == nil ? 1 : 2,
            "kind": kind,
            "bundleId": bundleID,
            "buildId": buildID,
            "runId": runID,
            "profileDigest": profileDigest,
            "startedAtMs": startedAtMs
        ]
        if let sanitation {
            guard validSanitationReceipt(sanitation, runID: runID) else {
                throw NSError(domain: "ReproRuntimeIdentity", code: 3)
            }
            object["sanitation"] = sanitation
        }
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        guard data.count <= maxBytes else {
            throw NSError(domain: "ReproRuntimeIdentity", code: 2)
        }
        return data
    }

    static func write(bundleID: String, buildID: String, runID: String,
                      profileDigest: String, startedAtMs: Int,
                      sanitation: [String: Any]? = nil, to base: URL) throws {
        let data = try markerData(bundleID: bundleID, buildID: buildID, runID: runID,
                                  profileDigest: profileDigest, startedAtMs: startedAtMs,
                                  sanitation: sanitation)
        let url = base.appendingPathComponent(filename)
        try FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
        try data.write(to: url, options: [.atomic])
        try FileManager.default.setAttributes([.posixPermissions: NSNumber(value: 0o600)],
                                               ofItemAtPath: url.path)
        let handle = try FileHandle(forWritingTo: url)
        try handle.synchronize()
        try handle.close()
    }

    private static func validBundleID(_ value: String) -> Bool {
        guard value.count <= 180 else { return false }
        return value.range(of: "^[A-Za-z][A-Za-z0-9-]*(\\.[A-Za-z][A-Za-z0-9-]*)+$",
                           options: .regularExpression) != nil
    }

    private static func validBuildID(_ value: String) -> Bool {
        guard value.count >= 8, value.count <= 128 else { return false }
        return value.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57) ||
            ($0.value >= 65 && $0.value <= 90) ||
            ($0.value >= 97 && $0.value <= 122) || $0.value == 95 || $0.value == 45
        }
    }

    private static func validUUID(_ value: String) -> Bool {
        guard let uuid = UUID(uuidString: value) else { return false }
        return uuid.uuidString.lowercased() == value
    }

    private static func validDigest(_ value: String) -> Bool {
        guard value.count == 64 else { return false }
        return value.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57) || ($0.value >= 97 && $0.value <= 102)
        }
    }

    private static func validSanitationReceipt(_ value: [String: Any], runID: String) -> Bool {
        guard Set(value.keys) == Set(["schemaVersion", "kind", "policyDigest", "runId", "stage",
                                      "startedAtMs", "completedAtMs", "pathCount",
                                      "userDefaultsKeyCount", "keychainItemCount", "status"]),
              integer(value["schemaVersion"]) == 1,
              value["kind"] as? String == "ios-app-sanitation-receipt",
              let policyDigest = value["policyDigest"] as? String, validDigest(policyDigest),
              value["runId"] as? String == runID,
              let stage = value["stage"] as? String, ["launch", "cleanup"].contains(stage),
              let started = integer(value["startedAtMs"]),
              let completed = integer(value["completedAtMs"]), completed >= started,
              let pathCount = integer(value["pathCount"]), (0...64).contains(pathCount),
              let defaultsCount = integer(value["userDefaultsKeyCount"]), (0...128).contains(defaultsCount),
              let keychainCount = integer(value["keychainItemCount"]), (0...32).contains(keychainCount),
              value["status"] as? String == "complete" else { return false }
        return true
    }

    private static func integer(_ value: Any?) -> Int? {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue >= 0,
              number.doubleValue.rounded(.towardZero) == number.doubleValue,
              number.doubleValue <= Double(Int.max) else { return nil }
        return number.intValue
    }
}
#endif
