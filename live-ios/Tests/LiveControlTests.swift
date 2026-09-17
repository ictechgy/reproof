import Foundation
import CoreFoundation
import CryptoKit
import Darwin
import Network
import UIKit
import XCTest

private let nativeProtocolVersion = 2
private let nativeHelperVersion = 2
private let nativeClockID = "ios-mach-continuous"
private let legacyLiveActions = ["tap", "long_press", "swipe", "text", "home", "reset"]
private let generalLiveActions = Set(["tap", "long_press", "swipe", "text", "home", "launch", "terminate"])

private struct GeneralRuntimeConfiguration {
    let profileDigest: String
    let applicationID: String
    let actions: [String]
}

private func generalRuntimeConfiguration(
    _ environment: [String: String] = ProcessInfo.processInfo.environment
) -> GeneralRuntimeConfiguration? {
    guard let digest = environment["REPRO_LIVE_GENERAL_PROFILE_DIGEST"],
          digest.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
          let applicationID = environment["REPRO_LIVE_APPLICATION_ID"],
          applicationID.range(of: "^[a-z][a-z0-9_-]{0,63}$", options: .regularExpression) != nil,
          let encodedActions = environment["REPRO_LIVE_GENERAL_ACTIONS"] else { return nil }
    let actions = encodedActions.split(separator: ",").map(String.init)
    guard !actions.isEmpty, actions.count == Set(actions).count,
          Set(actions).isSubset(of: generalLiveActions) else { return nil }
    return GeneralRuntimeConfiguration(
        profileDigest: digest, applicationID: applicationID, actions: actions)
}

private struct AnyCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init?(stringValue: String) {
        self.stringValue = stringValue
        intValue = nil
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

private func exactKeys<K: CodingKey & CaseIterable>(
    _ decoder: Decoder,
    _ type: K.Type
) throws where K.AllCases: Collection {
    let raw = try decoder.container(keyedBy: AnyCodingKey.self)
    let expected = Set(type.allCases.map(\.stringValue))
    guard Set(raw.allKeys.map(\.stringValue)) == expected else {
        throw BridgeFailure.invalidResponse
    }
}

private struct StrictJSONKeyScanner {
    private let bytes: [UInt8]
    private var index = 0

    init(_ data: Data) { bytes = Array(data) }

    mutating func validate() -> Bool {
        skipWhitespace()
        guard value(depth: 0) else { return false }
        skipWhitespace()
        return index == bytes.count
    }

    private mutating func skipWhitespace() {
        while index < bytes.count && [9, 10, 13, 32].contains(bytes[index]) { index += 1 }
    }

    private mutating func consume(_ byte: UInt8) -> Bool {
        guard index < bytes.count, bytes[index] == byte else { return false }
        index += 1
        return true
    }

    private mutating func string() -> String? {
        guard index < bytes.count, bytes[index] == 34 else { return nil }
        let start = index
        index += 1
        var escaped = false
        while index < bytes.count {
            let byte = bytes[index]
            index += 1
            if escaped { escaped = false; continue }
            if byte == 92 { escaped = true; continue }
            if byte == 34 {
                return try? JSONDecoder().decode(String.self, from: Data(bytes[start..<index]))
            }
            if byte < 32 { return nil }
        }
        return nil
    }

    private mutating func object(depth: Int) -> Bool {
        guard consume(123) else { return false }
        skipWhitespace()
        if consume(125) { return true }
        var keys = Set<String>()
        while true {
            skipWhitespace()
            guard let key = string(), keys.insert(key).inserted else { return false }
            skipWhitespace()
            guard consume(58) else { return false }
            skipWhitespace()
            guard value(depth: depth + 1) else { return false }
            skipWhitespace()
            if consume(125) { return true }
            guard consume(44) else { return false }
        }
    }

    private mutating func array(depth: Int) -> Bool {
        guard consume(91) else { return false }
        skipWhitespace()
        if consume(93) { return true }
        while true {
            guard value(depth: depth + 1) else { return false }
            skipWhitespace()
            if consume(93) { return true }
            guard consume(44) else { return false }
            skipWhitespace()
        }
    }

    private mutating func primitive() -> Bool {
        let start = index
        while index < bytes.count && ![9, 10, 13, 32, 44, 93, 125].contains(bytes[index]) {
            index += 1
        }
        return index > start
    }

    private mutating func value(depth: Int) -> Bool {
        guard depth <= 32, index < bytes.count else { return false }
        switch bytes[index] {
        case 123: return object(depth: depth)
        case 91: return array(depth: depth)
        case 34: return string() != nil
        default: return primitive()
        }
    }
}

private func uniqueJSONKeys(_ data: Data) -> Bool {
    var scanner = StrictJSONKeyScanner(data)
    return scanner.validate()
}

private func strictAuthorityNumberTypes(_ data: Data) -> Bool {
    guard uniqueJSONKeys(data),
          let root = try? JSONSerialization.jsonObject(with: data) else { return false }
    let integerKeys = Set(["protocolVersion", "sequence", "ownershipGeneration", "nativeDeadlineMs"])
    func visit(_ value: Any) -> Bool {
        if let object = value as? [String: Any] {
            if let authority = object["authority"] as? [String: Any] {
                for key in integerKeys {
                    guard let number = authority[key] as? NSNumber,
                          CFGetTypeID(number) != CFBooleanGetTypeID(),
                          !CFNumberIsFloatType(number) else { return false }
                }
            }
            return object.values.allSatisfy(visit)
        }
        if let array = value as? [Any] { return array.allSatisfy(visit) }
        return true
    }
    return visit(root)
}

private func nativeContinuousTimeMS() -> Int64 {
    var info = mach_timebase_info_data_t()
    mach_timebase_info(&info)
    let ticks = mach_continuous_time()
    let milliseconds = Double(ticks) * Double(info.numer) / Double(info.denom) / 1_000_000
    return Int64(min(milliseconds, Double(Int64.max)))
}

private final class NativeAuthorityContext {
    static let shared = NativeAuthorityContext()
    let enabled: Bool
    let valid: Bool
    let helperIncarnation: String?
    let hostIncarnation: String?
    let providerIncarnation: String?
    let nativeIncarnation = "native_" + UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "")

    private init() {
        let environment = ProcessInfo.processInfo.environment
        let keys = ["REPRO_LIVE_PROTOCOL_VERSION", "REPRO_LIVE_HELPER_VERSION",
                    "REPRO_LIVE_HELPER_INCARNATION", "REPRO_LIVE_HOST_INCARNATION",
                    "REPRO_LIVE_PROVIDER_INCARNATION"]
        enabled = keys.contains { environment[$0] != nil }
        helperIncarnation = environment[keys[2]]
        hostIncarnation = environment[keys[3]]
        providerIncarnation = environment[keys[4]]
        let pattern = try! NSRegularExpression(pattern: "^[a-z][a-z0-9_-]{0,63}$")
        func validID(_ value: String?) -> Bool {
            guard let value else { return false }
            return pattern.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) != nil
        }
        valid = !enabled || (environment[keys[0]] == String(nativeProtocolVersion) &&
            environment[keys[1]] == String(nativeHelperVersion) &&
            validID(helperIncarnation) && validID(hostIncarnation) && validID(providerIncarnation))
    }
}

final class LiveControlTests: XCTestCase {
    private let targetBundle = ProcessInfo.processInfo.environment["REPRO_TARGET_BUNDLE"]
        ?? "io.reproloop.sample.ios"
    private var targetApplication: XCUIApplication!
    private var springboard: XCUIApplication!
    private var isOnHomeScreen = false
    private var targetWasLaunched = false
    private var activeAutoRunID: String?
    private var activeAutoProfileDigest: String?
    private var activeSanitationPolicyDigest: String?
    private var authoritySequence = 0
    private var generalRuntime: GeneralRuntimeConfiguration?

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        let environment = ProcessInfo.processInfo.environment
        let generalRequested = environment["REPRO_LIVE_GENERAL_PROFILE_DIGEST"] != nil ||
            environment["REPRO_LIVE_APPLICATION_ID"] != nil ||
            environment["REPRO_LIVE_GENERAL_ACTIONS"] != nil
        generalRuntime = generalRuntimeConfiguration(environment)
        XCTAssertFalse(generalRequested && generalRuntime == nil, "general_profile_invalid")
        targetApplication = XCUIApplication(bundleIdentifier: targetBundle)
        springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    }

    func testCaptureFormats() throws {
        guard ProcessInfo.processInfo.environment["REPRO_CAPTURE_DIAGNOSTICS"] == "1" else {
            throw XCTSkip("Explicit sample capture diagnostics only")
        }
        XCTAssertEqual(targetBundle, "io.reproloop.sample.ios")
        defer { targetApplication.terminate() }
        for round in 0..<3 {
            launchTarget()
            XCTAssertTrue(targetApplication.wait(for: .runningForeground, timeout: 10))
            targetApplication.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.1788)).tap()
            targetApplication.typeText("QA")
            targetApplication.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.2955)).tap()
            targetApplication.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.3924)).tap()
            for frame in 0..<3 {
                let capture = XCUIScreen.main.screenshot()
                let converted = try XCTUnwrap(capture.image.jpegData(compressionQuality: 0.5))
                let png = XCTAttachment(data: capture.pngRepresentation, uniformTypeIdentifier: "public.png")
                png.name = "capture-\(round)-\(frame)-raw-png"
                png.lifetime = .keepAlways
                add(png)
                let jpeg = XCTAttachment(data: converted, uniformTypeIdentifier: "public.jpeg")
                jpeg.name = "capture-\(round)-\(frame)-converted-jpeg"
                jpeg.lifetime = .keepAlways
                add(jpeg)
                Thread.sleep(forTimeInterval: 0.5)
            }
            XCTAssertEqual(targetApplication.staticTexts["counter.count"].label, "2")
            targetApplication.terminate()
        }
    }

    func testControlSession() {
        guard let bridge = makeBridge() else {
            XCTFail("bridge_configuration_invalid")
            return
        }
        defer { bridge.shutdown() }

        let authority = NativeAuthorityContext.shared
        guard authority.valid else {
            XCTFail("bridge_configuration_invalid")
            return
        }
        var startupAuthority: AuthorityGrant?
        if authority.enabled {
            do { startupAuthority = try bridge.ready() } catch {
                XCTFail("bridge_request_failed")
                return
            }
        }

        if authority.enabled {
            guard startupAuthority?.matches(authority, requireLive: true) == true else {
                XCTFail("authority_expired")
                return
            }
        }
        defer {
            if targetWasLaunched {
                targetApplication.terminate()
            }
        }
        targetWasLaunched = true
        launchTarget()
        guard targetApplication.wait(for: .runningForeground, timeout: 10) else {
            XCTFail("target_unavailable")
            return
        }
        if authority.enabled {
            do { try bridge.started(authority: startupAuthority) } catch {
                XCTFail("bridge_request_failed")
                return
            }
        }

        var failure: BridgeFailure?
        do {
            if !authority.enabled { _ = try bridge.ready() }
            try sendFrame(using: bridge)

            let deadline = Date().addingTimeInterval(15 * 60)
            var lastFrameAt = Date()
            var stopped = false
            var draining = false

            while Date() < deadline {
                let envelope = try bridge.next()
                if envelope.stop {
                    stopped = true
                    break
                }

                if let command = envelope.command {
                    guard !draining || command.action == "authority_retire" else { throw BridgeFailure.invalidResponse }
                    draining = command.action == "authority_cleanup" || command.action == "authority_retire"
                    let result = execute(command)
                    if !draining {
                        Thread.sleep(forTimeInterval: 0.15)
                        try sendFrame(using: bridge)
                    }
                    try bridge.ack(
                        id: command.id,
                        ok: result.ok,
                        error: result.error,
                        timing: "best-effort",
                        authority: command.authority,
                        cleanupEvidence: result.cleanupEvidence
                    )
                    lastFrameAt = Date()
                } else if !draining && Date().timeIntervalSince(lastFrameAt) >= 0.2 {
                    try sendFrame(using: bridge)
                    lastFrameAt = Date()
                }

                Thread.sleep(forTimeInterval: 0.05)
            }

            if !stopped {
                failure = .timeout
            }
        } catch let error as BridgeFailure {
            failure = error
        } catch {
            failure = .requestFailed
        }

        if let failure {
            XCTFail(failure.safeCode)
        }
    }

    private func launchTarget(autoRunID: String? = nil) {
        let environment = ProcessInfo.processInfo.environment
        if generalRuntime != nil {
            var launchEnvironment: [String: String] = [:]
            if let digest = environment["REPRO_LIVE_AUTO_PROFILE_DIGEST"],
               digest.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
               let runID = autoRunID ?? environment["REPRO_LIVE_AUTO_RUN_ID"],
               UUID(uuidString: runID)?.uuidString.lowercased() == runID {
                activeAutoRunID = runID
                activeAutoProfileDigest = digest
                launchEnvironment = ["REPRO_MODE": "observe", "REPRO_RUN_ID": runID,
                                     "REPRO_AUTO_PROFILE_DIGEST": digest]
            }
            if let policy = environment["REPRO_LIVE_SANITATION_POLICY_DIGEST"] {
                guard policy.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                      activeAutoRunID != nil, activeAutoProfileDigest != nil else {
                    XCTFail("sanitation_configuration_invalid")
                    return
                }
                activeSanitationPolicyDigest = policy
                launchEnvironment["REPRO_SANITATION_POLICY_DIGEST"] = policy
            }
            targetApplication.launchEnvironment = launchEnvironment
            targetApplication.launch()
            return
        }
        let recording = environment["REPRO_LIVE_RECORD_SDK"] == "1"
        let requestedCase = environment["REPRO_LIVE_CASE"] ?? "counter"
        let fixture = ["counter", "duplicate-submit", "reset"].contains(requestedCase) ? requestedCase : "counter"
        let runID = autoRunID ?? environment["REPRO_LIVE_AUTO_RUN_ID"] ?? UUID().uuidString
        activeAutoRunID = runID
        activeAutoProfileDigest = environment["REPRO_LIVE_AUTO_PROFILE_DIGEST"]
        var launchEnvironment = [
            "REPRO_MODE": recording ? "record" : "replay",
            "REPRO_CASE": fixture,
            "REPRO_FIXTURE_RESET": "1",
            "REPRO_RUN_ID": runID
        ]
        if let digest = activeAutoProfileDigest {
            launchEnvironment["REPRO_AUTO_PROFILE_DIGEST"] = digest
        }
        if let policy = environment["REPRO_LIVE_SANITATION_POLICY_DIGEST"] {
            guard policy.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
                  activeAutoProfileDigest != nil else {
                XCTFail("sanitation_configuration_invalid")
                return
            }
            activeSanitationPolicyDigest = policy
            launchEnvironment["REPRO_SANITATION_POLICY_DIGEST"] = policy
        }
        targetApplication.launchEnvironment = launchEnvironment
        targetApplication.launch()
    }

    private func execute(_ command: LiveCommand) -> CommandResult {
        guard admitAuthority(command) else { return .failure("authority_rejected") }
        if let runtime = generalRuntime,
           command.action != "authority_cleanup", command.action != "authority_retire",
           !runtime.actions.contains(command.action) {
            return .failure("unsupported_action")
        }
        switch command.action {
        case "tap":
            guard let point = normalizedPoint(command.payload, xKey: "x", yKey: "y") else {
                return .failure("invalid_bounds")
            }
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            activeApplication.coordinate(withNormalizedOffset: point).tap()
            return .success

        case "long_press":
            guard let point = normalizedPoint(command.payload, xKey: "x", yKey: "y") else {
                return .failure("invalid_bounds")
            }
            guard let duration = duration(command.payload) else {
                return .failure("invalid_duration")
            }
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            activeApplication.coordinate(withNormalizedOffset: point).press(forDuration: duration)
            return .success

        case "swipe":
            guard let from = normalizedPoint(command.payload, xKey: "fromX", yKey: "fromY"),
                  let to = normalizedPoint(command.payload, xKey: "toX", yKey: "toY") else {
                return .failure("invalid_bounds")
            }
            guard duration(command.payload) != nil else {
                return .failure("invalid_duration")
            }
            // XCTest exposes a gesture-batch drag, but no stable cross-version control over
            // swipe velocity. The requested duration is validated; execution is best-effort.
            let start = activeApplication.coordinate(withNormalizedOffset: from)
            let end = activeApplication.coordinate(withNormalizedOffset: to)
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            start.press(forDuration: 0.01, thenDragTo: end)
            return .success

        case "text":
            guard !isOnHomeScreen,
                  let value = command.payload["value"]?.stringValue,
                  value.count <= 256 else {
                return .failure("invalid_text")
            }
            // The target keyboard must already be focused by the user or preceding tap.
            // Do not inspect or record the text value.
            if !value.isEmpty {
                guard effectAuthorized(command) else { return .failure("authority_expired") }
                targetApplication.typeText(value)
            }
            return .success

        case "home":
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            XCUIDevice.shared.press(.home)
            isOnHomeScreen = true
            return .success

        case "launch":
            guard let runtime = generalRuntime,
                  Set(command.payload.keys) == (activeAutoProfileDigest == nil
                      ? Set(["applicationId"]) : Set(["applicationId", "autoRunId"])),
                  command.payload["applicationId"]?.stringValue == runtime.applicationID,
                  effectAuthorized(command) else { return .failure("invalid_config") }
            let nextRunID = command.payload["autoRunId"]?.stringValue
            if activeAutoProfileDigest != nil {
                guard let nextRunID, UUID(uuidString: nextRunID)?.uuidString.lowercased() == nextRunID else {
                    return .failure("invalid_config")
                }
            }
            launchTarget(autoRunID: nextRunID)
            guard targetApplication.wait(for: .runningForeground, timeout: 10) else {
                return .failure("target_unavailable")
            }
            isOnHomeScreen = false
            targetWasLaunched = true
            return .success

        case "terminate":
            guard let runtime = generalRuntime,
                  Set(command.payload.keys) == Set(["applicationId"]),
                  command.payload["applicationId"]?.stringValue == runtime.applicationID,
                  effectAuthorized(command) else { return .failure("invalid_config") }
            targetApplication.terminate()
            targetWasLaunched = false
            return .success

        case "reset":
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            targetApplication.terminate()
            let nextRunID = command.payload["autoRunId"]?.stringValue
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            launchTarget(autoRunID: nextRunID)
            guard targetApplication.wait(for: .runningForeground, timeout: 10) else {
                return .failure("target_unavailable")
            }
            isOnHomeScreen = false
            targetWasLaunched = true
            return .success

        case "report_capture":
            guard ProcessInfo.processInfo.environment["REPRO_LIVE_RECORD_SDK"] == "1",
                  targetBundle == "io.reproloop.sample.ios", !isOnHomeScreen else {
                return .failure("capture_unavailable")
            }
            if let profileDigest = activeAutoProfileDigest {
                guard let runID = activeAutoRunID,
                      UUID(uuidString: runID) != nil,
                      UUID(uuidString: runID)?.uuidString.lowercased() == runID.lowercased(),
                      profileDigest.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
                    return .failure("capture_invalid")
                }
                guard effectAuthorized(command) else { return .failure("authority_expired") }
                return freezeAutoCapture(runID: runID)
            }
            let report = targetApplication.buttons["counter.report"]
            guard report.exists && report.isHittable else { return .failure("capture_unavailable") }
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            report.tap()
            let ready = targetApplication.staticTexts["counter.capture_ready"]
            guard ready.waitForExistence(timeout: 5), ready.label == "Capture ready" else {
                return .failure("capture_invalid")
            }
            return .success

        case "authority_retire":
            guard command.payload.isEmpty, effectAuthorized(command) else {
                return .failure("authority_rejected")
            }
            targetApplication.terminate()
            guard let timeout = cleanupWaitTimeout(command),
                  targetApplication.wait(for: .notRunning, timeout: timeout) else {
                return .failure("target_not_stopped")
            }
            targetWasLaunched = false
            return .success

        case "authority_cleanup":
            guard command.payload.isEmpty, effectAuthorized(command) else {
                return .failure("authority_rejected")
            }
            if activeSanitationPolicyDigest != nil {
                guard let runID = activeAutoRunID,
                      UUID(uuidString: runID)?.uuidString.lowercased() == runID,
                      effectAuthorized(command) else { return .failure("sanitation_configuration_invalid") }
                CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(),
                    CFNotificationName(rawValue: ("io.reproloop.sanitize." + runID) as CFString),
                    nil, nil, true)
            } else if targetWasLaunched {
                targetApplication.terminate()
            }
            guard let timeout = cleanupWaitTimeout(command),
                  targetApplication.wait(for: .notRunning, timeout: timeout) else {
                return .failure("target_not_stopped")
            }
            guard effectAuthorized(command) else { return .failure("authority_expired") }
            targetWasLaunched = false
            return .cleanupSuccess(CleanupTerminationEvidence(
                bundleID: targetBundle,
                state: "not-running",
                observer: "xctest-application-state"))

        default:
            return .failure("unsupported_action")
        }
    }

    private func admitAuthority(_ command: LiveCommand) -> Bool {
        let context = NativeAuthorityContext.shared
        if !context.enabled { return command.authority == nil }
        guard let grant = command.authority, grant.matches(context, requireLive: true),
              grant.operationID == command.id, grant.sequence > authoritySequence else { return false }
        authoritySequence = grant.sequence
        return true
    }

    private func effectAuthorized(_ command: LiveCommand) -> Bool {
        let context = NativeAuthorityContext.shared
        return !context.enabled ? command.authority == nil : command.authority?.matches(context, requireLive: true) == true
    }

    private func cleanupWaitTimeout(_ command: LiveCommand) -> TimeInterval? {
        guard let authority = command.authority else { return 10 }
        let now = nativeContinuousTimeMS()
        guard authority.nativeDeadlineMS > now else { return nil }
        let remaining = Double(authority.nativeDeadlineMS - now) / 1000
        return min(10, remaining)
    }

    private func freezeAutoCapture(runID: String) -> CommandResult {
        let normalized = runID.lowercased()
        let waiter = DarwinAutoAckWaiter(runID: normalized)
        guard waiter.postAndWait(timeout: 10) else {
            return .failure("capture_invalid")
        }
        return .success
    }

    private var activeApplication: XCUIApplication {
        isOnHomeScreen ? springboard : targetApplication
    }

    private func normalizedPoint(
        _ payload: [String: JSONValue],
        xKey: String,
        yKey: String
    ) -> CGVector? {
        guard let x = payload[xKey]?.doubleValue,
              let y = payload[yKey]?.doubleValue,
              x.isFinite,
              y.isFinite,
              (0...1).contains(x),
              (0...1).contains(y) else {
            return nil
        }
        return CGVector(dx: x, dy: y)
    }

    private func duration(_ payload: [String: JSONValue]) -> TimeInterval? {
        guard let milliseconds = payload["durationMs"]?.doubleValue,
              milliseconds.isFinite,
              (50...3000).contains(milliseconds) else {
            return nil
        }
        return milliseconds / 1000
    }

    private func sendFrame(using bridge: LiveBridge) throws {
        try bridge.synchronizeClock()
        let captureStartMs = nativeContinuousTimeMS()
        let screenshot = XCUIScreen.main.screenshot()
        let captureEndMs = nativeContinuousTimeMS()
        let image = screenshot.image
        guard let cgImage = image.cgImage,
              let jpeg = image.jpegData(compressionQuality: 0.5) else {
            throw BridgeFailure.captureFailed
        }

        let width = cgImage.width
        let height = cgImage.height
        let orientation = width >= height ? "landscape" : "portrait"
        try bridge.frame(
            imageBase64: jpeg.base64EncodedString(),
            width: width,
            height: height,
            logicalWidth: Int(image.size.width.rounded()),
            logicalHeight: Int(image.size.height.rounded()),
            orientation: orientation,
            capturedAt: Int64(Date().timeIntervalSince1970 * 1000),
            nativeTiming: NativeAuthorityContext.shared.enabled
                ? NativeFrameTiming(
                    version: 1,
                    nativeClockID: nativeClockID,
                    nativeIncarnation: NativeAuthorityContext.shared.nativeIncarnation,
                    captureStartMs: captureStartMs,
                    captureEndMs: captureEndMs)
                : nil
        )
    }
}

private func makeBridge() -> LiveBridge? {
    let environment = ProcessInfo.processInfo.environment
    let hasNativeConfiguration = environment["REPRO_LIVE_LISTEN_HOST"] != nil ||
        environment["REPRO_LIVE_LISTEN_PORT"] != nil
    if hasNativeConfiguration {
        return NativeHTTPBridge(environment: environment)
    }
    return BridgeClient()
}

private final class DarwinAutoAckWaiter {
    private let center = CFNotificationCenterGetDarwinNotifyCenter()
    private let request: String
    private let finalized: String
    private let invalid: String
    private var observer: UnsafeRawPointer { UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque()) }
    private let lock = NSLock()
    private var outcome: String?

    init(runID: String) {
        request = "io.reproloop.auto.freeze.\(runID)"
        finalized = "io.reproloop.auto.finalized.\(runID)"
        invalid = "io.reproloop.auto.invalid.\(runID)"
        CFNotificationCenterAddObserver(center, observer, { _, observer, name, _, _ in
            guard let observer, let name else { return }
            let waiter = Unmanaged<DarwinAutoAckWaiter>.fromOpaque(UnsafeMutableRawPointer(mutating: observer)).takeUnretainedValue()
            waiter.receive(name.rawValue as String)
        }, finalized as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(center, observer, { _, observer, name, _, _ in
            guard let observer, let name else { return }
            let waiter = Unmanaged<DarwinAutoAckWaiter>.fromOpaque(UnsafeMutableRawPointer(mutating: observer)).takeUnretainedValue()
            waiter.receive(name.rawValue as String)
        }, invalid as CFString, nil, .deliverImmediately)
    }

    deinit {
        CFNotificationCenterRemoveObserver(center, observer, CFNotificationName(rawValue: finalized as CFString), nil)
        CFNotificationCenterRemoveObserver(center, observer, CFNotificationName(rawValue: invalid as CFString), nil)
    }

    func postAndWait(timeout: TimeInterval) -> Bool {
        CFNotificationCenterPostNotification(center, CFNotificationName(rawValue: request as CFString), nil, nil, true)
        let deadline = Date().addingTimeInterval(min(timeout, 10))
        while Date() < deadline {
            lock.lock()
            let received = outcome
            lock.unlock()
            if received != nil { return received == finalized }
            _ = RunLoop.current.run(mode: .default, before: min(deadline, Date().addingTimeInterval(0.05)))
        }
        return false
    }

    private func receive(_ name: String) {
        lock.lock()
        if outcome == nil { outcome = name }
        lock.unlock()
    }
}

private struct CleanupTerminationEvidence: Codable {
    let bundleID: String
    let state: String
    let observer: String

    private enum CodingKeys: String, CodingKey {
        case bundleID = "bundleId"
        case state
        case observer
    }

    var wireObject: [String: Any] {
        ["bundleId": bundleID, "state": state, "observer": observer]
    }
}

private struct CommandResult {
    let ok: Bool
    let error: String?
    let cleanupEvidence: CleanupTerminationEvidence?

    static var success: CommandResult {
        CommandResult(ok: true, error: nil, cleanupEvidence: nil)
    }

    static func cleanupSuccess(_ evidence: CleanupTerminationEvidence) -> CommandResult {
        CommandResult(ok: true, error: nil, cleanupEvidence: evidence)
    }

    static func failure(_ error: String) -> CommandResult {
        CommandResult(ok: false, error: error, cleanupEvidence: nil)
    }
}

private struct AuthorityGrant: Codable {
    let protocolVersion: Int
    let operationID: String
    let operationFingerprint: String
    let payloadDigest: String
    let projectID: String
    let sessionID: String
    let controllerID: String
    let sequence: Int
    let ownershipGeneration: Int
    let hostIncarnation: String
    let helperIncarnation: String
    let providerIncarnation: String
    let nativeIncarnation: String
    let nativeClockID: String
    let nativeDeadlineMS: Int64

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case protocolVersion, operationFingerprint, payloadDigest, sequence, ownershipGeneration
        case operationID = "operationId"
        case projectID = "projectId"
        case sessionID = "sessionId"
        case controllerID = "controllerId"
        case hostIncarnation, helperIncarnation, providerIncarnation, nativeIncarnation
        case nativeClockID = "nativeClockId"
        case nativeDeadlineMS = "nativeDeadlineMs"
    }

    init(from decoder: Decoder) throws {
        try exactKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        protocolVersion = try container.decode(Int.self, forKey: .protocolVersion)
        operationID = try container.decode(String.self, forKey: .operationID)
        operationFingerprint = try container.decode(String.self, forKey: .operationFingerprint)
        payloadDigest = try container.decode(String.self, forKey: .payloadDigest)
        projectID = try container.decode(String.self, forKey: .projectID)
        sessionID = try container.decode(String.self, forKey: .sessionID)
        controllerID = try container.decode(String.self, forKey: .controllerID)
        sequence = try container.decode(Int.self, forKey: .sequence)
        ownershipGeneration = try container.decode(Int.self, forKey: .ownershipGeneration)
        hostIncarnation = try container.decode(String.self, forKey: .hostIncarnation)
        helperIncarnation = try container.decode(String.self, forKey: .helperIncarnation)
        providerIncarnation = try container.decode(String.self, forKey: .providerIncarnation)
        nativeIncarnation = try container.decode(String.self, forKey: .nativeIncarnation)
        nativeClockID = try container.decode(String.self, forKey: .nativeClockID)
        nativeDeadlineMS = try container.decode(Int64.self, forKey: .nativeDeadlineMS)
        let idPattern = "^[a-z][a-z0-9_-]{0,63}$"
        let digestPattern = "^[0-9a-f]{64}$"
        guard protocolVersion == nativeProtocolVersion, sequence > 0, ownershipGeneration > 0,
              nativeDeadlineMS >= 0,
              operationFingerprint.range(of: digestPattern, options: .regularExpression) != nil,
              payloadDigest.range(of: digestPattern, options: .regularExpression) != nil,
              [operationID, projectID, sessionID, controllerID, hostIncarnation, helperIncarnation,
               providerIncarnation, nativeIncarnation, nativeClockID].allSatisfy({
                   $0.range(of: idPattern, options: .regularExpression) != nil
               }) else { throw BridgeFailure.invalidResponse }
    }

    func matches(_ context: NativeAuthorityContext, requireLive: Bool) -> Bool {
        protocolVersion == nativeProtocolVersion && helperIncarnation == context.helperIncarnation &&
            hostIncarnation == context.hostIncarnation && providerIncarnation == context.providerIncarnation &&
            nativeIncarnation == context.nativeIncarnation && nativeClockID == "ios-mach-continuous" &&
            (!requireLive || nativeContinuousTimeMS() < nativeDeadlineMS)
    }

    var wireObject: [String: Any] {
        ["protocolVersion": protocolVersion, "operationId": operationID,
         "operationFingerprint": operationFingerprint, "payloadDigest": payloadDigest,
         "projectId": projectID, "sessionId": sessionID, "controllerId": controllerID,
         "sequence": sequence, "ownershipGeneration": ownershipGeneration,
         "hostIncarnation": hostIncarnation, "helperIncarnation": helperIncarnation,
         "providerIncarnation": providerIncarnation, "nativeIncarnation": nativeIncarnation,
         "nativeClockId": nativeClockID, "nativeDeadlineMs": nativeDeadlineMS]
    }
}

private struct NativeFrameTiming: Encodable {
    let version: Int
    let nativeClockID: String
    let nativeIncarnation: String
    let captureStartMs: Int64
    let captureEndMs: Int64

    private enum CodingKeys: String, CodingKey {
        case version
        case nativeClockID = "nativeClockId"
        case nativeIncarnation
        case captureStartMs
        case captureEndMs
    }

    var wireObject: [String: Any] {
        ["version": version, "nativeClockId": nativeClockID,
         "nativeIncarnation": nativeIncarnation, "captureStartMs": captureStartMs,
         "captureEndMs": captureEndMs]
    }
}

private struct LiveCommand: Decodable {
    let id: String
    let action: String
    let payload: [String: JSONValue]
    let authority: AuthorityGrant?

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case id
        case action
        case payload
        case authority
    }

    init(id: String, action: String, payload: [String: JSONValue]) {
        self.id = id
        self.action = action
        self.payload = payload
        self.authority = nil
    }

    init(from decoder: Decoder) throws {
        let raw = try decoder.container(keyedBy: AnyCodingKey.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        action = try container.decode(String.self, forKey: .action)
        payload = try container.decodeIfPresent([String: JSONValue].self, forKey: .payload) ?? [:]
        authority = try container.decodeIfPresent(AuthorityGrant.self, forKey: .authority)
        let expected = Set([CodingKeys.id.stringValue, CodingKeys.action.stringValue, CodingKeys.payload.stringValue] +
            (NativeAuthorityContext.shared.enabled ? [CodingKeys.authority.stringValue] : []))
        guard !id.isEmpty, id.count <= 256, !action.isEmpty, action.count <= 64 else {
            throw BridgeFailure.invalidResponse
        }
        guard Set(raw.allKeys.map(\.stringValue)) == expected,
              NativeAuthorityContext.shared.enabled == (authority != nil) else {
            throw BridgeFailure.invalidResponse
        }
    }
}

private struct NextEnvelope: Decodable {
    let command: LiveCommand?
    let stop: Bool

    private enum CodingKeys: String, CodingKey, CaseIterable {
        case command
        case stop
    }

    init(command: LiveCommand?, stop: Bool) {
        self.command = command
        self.stop = stop
    }

    init(from decoder: Decoder) throws {
        try exactKeys(decoder, CodingKeys.self)
        let container = try decoder.container(keyedBy: CodingKeys.self)
        command = try container.decodeIfPresent(LiveCommand.self, forKey: .command)
        stop = try container.decodeIfPresent(Bool.self, forKey: .stop) ?? false
    }
}

private enum JSONValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            throw BridgeFailure.invalidResponse
        }
    }

    var doubleValue: Double? {
        if case let .number(value) = self { return value }
        return nil
    }

    var stringValue: String? {
        if case let .string(value) = self { return value }
        return nil
    }
}

private enum BridgeFailure: Error {
    case configurationInvalid
    case timeout
    case requestFailed
    case serverReturnedError
    case invalidResponse
    case captureFailed

    var safeCode: String {
        switch self {
        case .configurationInvalid: return "bridge_configuration_invalid"
        case .timeout: return "bridge_timeout"
        case .requestFailed: return "bridge_request_failed"
        case .serverReturnedError: return "bridge_server_error"
        case .invalidResponse: return "bridge_invalid_response"
        case .captureFailed: return "frame_capture_failed"
        }
    }
}

private protocol LiveBridge: AnyObject {
    func next() throws -> NextEnvelope
    func ready() throws -> AuthorityGrant?
    func started(authority: AuthorityGrant?) throws
    func synchronizeClock() throws
    func frame(
        imageBase64: String,
        width: Int,
        height: Int,
        logicalWidth: Int,
        logicalHeight: Int,
        orientation: String,
        capturedAt: Int64,
        nativeTiming: NativeFrameTiming?
    ) throws
    func ack(id: String, ok: Bool, error: String?, timing: String, authority: AuthorityGrant?,
             cleanupEvidence: CleanupTerminationEvidence?) throws
    func shutdown()
}

private final class BridgeClient: LiveBridge {
    private struct ReadyPayload: Encodable {
        struct Capabilities: Encodable {
            let actions: [String]
            let inputMode: String
            let media: String
            let nativeFrameTimingVersion: Int?

            enum CodingKeys: String, CodingKey {
                case actions
                case inputMode = "inputMode"
                case media
                case nativeFrameTimingVersion
            }

            func encode(to encoder: Encoder) throws {
                var container = encoder.container(keyedBy: CodingKeys.self)
                try container.encode(actions, forKey: .actions)
                try container.encode(inputMode, forKey: .inputMode)
                try container.encode(media, forKey: .media)
                try container.encodeIfPresent(nativeFrameTimingVersion, forKey: .nativeFrameTimingVersion)
            }
        }

        let capabilities: Capabilities
        let protocolVersion: Int?
        let helperVersion: Int?
        let helperIncarnation: String?
        let hostIncarnation: String?
        let providerIncarnation: String?
        let nativeIncarnation: String?
        let nativeClockId: String?
        let nativeTimeMs: Int64?
    }

    private struct FramePayload: Encodable {
        let imageBase64: String
        let mime: String
        let width: Int
        let height: Int
        let logicalWidth: Int
        let logicalHeight: Int
        let orientation: String
        let capturedAt: Int64
        let nativeFrameId: Int64
        let nativeTiming: NativeFrameTiming?

        enum CodingKeys: String, CodingKey {
            case imageBase64
            case mime
            case width
            case height
            case logicalWidth
            case logicalHeight
            case orientation
            case capturedAt
            case nativeFrameId
            case nativeTiming
        }

        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(imageBase64, forKey: .imageBase64)
            try container.encode(mime, forKey: .mime)
            try container.encode(width, forKey: .width)
            try container.encode(height, forKey: .height)
            try container.encode(logicalWidth, forKey: .logicalWidth)
            try container.encode(logicalHeight, forKey: .logicalHeight)
            try container.encode(orientation, forKey: .orientation)
            try container.encode(capturedAt, forKey: .capturedAt)
            try container.encode(nativeFrameId, forKey: .nativeFrameId)
            try container.encodeIfPresent(nativeTiming, forKey: .nativeTiming)
        }
    }

    private struct ClockStartPayload: Encodable {
        let nativeClockID: String
        let nativeIncarnation: String
        let nativeSendMs: Int64

        private enum CodingKeys: String, CodingKey {
            case nativeClockID = "nativeClockId"
            case nativeIncarnation
            case nativeSendMs
        }
    }

    private struct ClockEndPayload: Encodable {
        let exchangeID: String
        let nativeReceiveMs: Int64

        private enum CodingKeys: String, CodingKey {
            case exchangeID = "exchangeId"
            case nativeReceiveMs
        }
    }

    private struct ClockStartResponse: Decodable {
        let exchangeID: String
        private enum CodingKeys: String, CodingKey, CaseIterable {
            case ok
            case exchangeID = "exchangeId"
        }

        init(from decoder: Decoder) throws {
            try exactKeys(decoder, CodingKeys.self)
            let container = try decoder.container(keyedBy: CodingKeys.self)
            guard try container.decode(Bool.self, forKey: .ok) else {
                throw BridgeFailure.invalidResponse
            }
            exchangeID = try container.decode(String.self, forKey: .exchangeID)
            guard !exchangeID.isEmpty, exchangeID.count <= 256 else {
                throw BridgeFailure.invalidResponse
            }
        }
    }

    private struct ClockEndResponse: Decodable {
        private enum CodingKeys: String, CodingKey, CaseIterable { case ok }

        init(from decoder: Decoder) throws {
            try exactKeys(decoder, CodingKeys.self)
            let container = try decoder.container(keyedBy: CodingKeys.self)
            guard try container.decode(Bool.self, forKey: .ok) else {
                throw BridgeFailure.invalidResponse
            }
        }
    }

    private struct StartedPayload: Encodable {
        let authority: AuthorityGrant
    }

    private struct ReadyResponse: Decodable {
        let ok: Bool
        let authority: AuthorityGrant?

        private enum CodingKeys: String, CodingKey { case ok, authority }

        init(from decoder: Decoder) throws {
            let raw = try decoder.container(keyedBy: AnyCodingKey.self)
            let expected = Set([CodingKeys.ok.stringValue] +
                (NativeAuthorityContext.shared.enabled ? [CodingKeys.authority.stringValue] : []))
            guard Set(raw.allKeys.map(\.stringValue)) == expected else {
                throw BridgeFailure.invalidResponse
            }
            let container = try decoder.container(keyedBy: CodingKeys.self)
            ok = try container.decode(Bool.self, forKey: .ok)
            authority = try container.decodeIfPresent(AuthorityGrant.self, forKey: .authority)
            guard ok, NativeAuthorityContext.shared.enabled == (authority != nil) else {
                throw BridgeFailure.invalidResponse
            }
        }
    }

    private struct AckPayload: Encodable {
        let id: String
        let ok: Bool
        let error: String?
        let timing: String
        let authority: AuthorityGrant?
        let cleanupEvidence: CleanupTerminationEvidence?

        enum CodingKeys: String, CodingKey {
            case id
            case ok
            case error
            case timing
            case authority
            case cleanupEvidence
        }

        func encode(to encoder: Encoder) throws {
            var container = encoder.container(keyedBy: CodingKeys.self)
            try container.encode(id, forKey: .id)
            try container.encode(ok, forKey: .ok)
            try container.encodeIfPresent(error, forKey: .error)
            try container.encode(timing, forKey: .timing)
            try container.encodeIfPresent(authority, forKey: .authority)
            try container.encodeIfPresent(cleanupEvidence, forKey: .cleanupEvidence)
        }
    }

    private let baseURL: URL
    private let token: String
    private let session: URLSession
    private let encoder = JSONEncoder()
    private let requestTimeout: TimeInterval = 5
    private var frameCounter: Int64 = 0
    private var lastClockSyncMs: Int64?

    init?() {
        guard let rawURL = ProcessInfo.processInfo.environment["REPRO_LIVE_URL"],
              let url = URL(string: rawURL),
              let scheme = url.scheme,
              ["http", "https"].contains(scheme),
              url.host != nil,
              let token = ProcessInfo.processInfo.environment["REPRO_LIVE_TOKEN"],
              !token.isEmpty,
              !token.contains(where: { $0 == "\n" || $0 == "\r" }) else {
            return nil
        }
        baseURL = url
        self.token = token
        let configuration = URLSessionConfiguration.ephemeral
        configuration.waitsForConnectivity = false
        configuration.timeoutIntervalForRequest = requestTimeout
        configuration.timeoutIntervalForResource = requestTimeout
        session = URLSession(configuration: configuration)
    }

    func next() throws -> NextEnvelope {
        let data = try request(method: "GET", path: "next", body: nil)
        guard strictAuthorityNumberTypes(data) else { throw BridgeFailure.invalidResponse }
        do {
            return try JSONDecoder().decode(NextEnvelope.self, from: data)
        } catch let error as BridgeFailure {
            throw error
        } catch {
            throw BridgeFailure.invalidResponse
        }
    }

    func ready() throws -> AuthorityGrant? {
        let authority = NativeAuthorityContext.shared
        let actions = generalRuntimeConfiguration()?.actions ?? legacyLiveActions
        let payload = ReadyPayload(
            capabilities: .init(actions: actions,
                                inputMode: "gesture-batch", media: "sampled-jpeg",
                                nativeFrameTimingVersion: authority.enabled ? 1 : nil),
            protocolVersion: authority.enabled ? nativeProtocolVersion : nil,
            helperVersion: authority.enabled ? nativeHelperVersion : nil,
            helperIncarnation: authority.enabled ? authority.helperIncarnation : nil,
            hostIncarnation: authority.enabled ? authority.hostIncarnation : nil,
            providerIncarnation: authority.enabled ? authority.providerIncarnation : nil,
            nativeIncarnation: authority.enabled ? authority.nativeIncarnation : nil,
            nativeClockId: authority.enabled ? nativeClockID : nil,
            nativeTimeMs: authority.enabled ? nativeContinuousTimeMS() : nil)
        let data = try post(path: "ready", payload: payload)
        guard strictAuthorityNumberTypes(data) else { throw BridgeFailure.invalidResponse }
        do {
            return try JSONDecoder().decode(ReadyResponse.self, from: data).authority
        } catch let error as BridgeFailure {
            throw error
        } catch {
            throw BridgeFailure.invalidResponse
        }
    }

    func started(authority: AuthorityGrant?) throws {
        guard let authority else { throw BridgeFailure.invalidResponse }
        _ = try post(path: "started", payload: StartedPayload(authority: authority))
    }

    func synchronizeClock() throws {
        let authority = NativeAuthorityContext.shared
        guard authority.enabled else { return }
        let now = nativeContinuousTimeMS()
        guard now >= 0 else { throw BridgeFailure.invalidResponse }
        if let lastClockSyncMs,
           now >= lastClockSyncMs,
           now - lastClockSyncMs < 30_000 {
            return
        }

        let nativeSendMs = nativeContinuousTimeMS()
        guard nativeSendMs >= 0 else { throw BridgeFailure.invalidResponse }
        let startBody: Data
        do {
            startBody = try encoder.encode(ClockStartPayload(
                nativeClockID: nativeClockID,
                nativeIncarnation: authority.nativeIncarnation,
                nativeSendMs: nativeSendMs))
        } catch {
            throw BridgeFailure.invalidResponse
        }
        let startData = try request(method: "POST", path: "clock-start", body: startBody)
        let nativeReceiveMs = nativeContinuousTimeMS()
        guard nativeReceiveMs >= nativeSendMs else { throw BridgeFailure.invalidResponse }
        guard uniqueJSONKeys(startData) else { throw BridgeFailure.invalidResponse }
        let exchangeID: String
        do {
            exchangeID = try JSONDecoder().decode(ClockStartResponse.self, from: startData).exchangeID
        } catch let error as BridgeFailure {
            throw error
        } catch {
            throw BridgeFailure.invalidResponse
        }

        let endBody: Data
        do {
            endBody = try encoder.encode(ClockEndPayload(
                exchangeID: exchangeID,
                nativeReceiveMs: nativeReceiveMs))
        } catch {
            throw BridgeFailure.invalidResponse
        }
        let endData = try request(method: "POST", path: "clock-end", body: endBody)
        guard uniqueJSONKeys(endData) else { throw BridgeFailure.invalidResponse }
        do {
            _ = try JSONDecoder().decode(ClockEndResponse.self, from: endData)
        } catch let error as BridgeFailure {
            throw error
        } catch {
            throw BridgeFailure.invalidResponse
        }
        lastClockSyncMs = nativeReceiveMs
    }

    func frame(
        imageBase64: String,
        width: Int,
        height: Int,
        logicalWidth: Int,
        logicalHeight: Int,
        orientation: String,
        capturedAt: Int64,
        nativeTiming: NativeFrameTiming?
    ) throws {
        let authority = NativeAuthorityContext.shared
        if authority.enabled {
            guard let nativeTiming,
                  nativeTiming.version == 1,
                  nativeTiming.nativeClockID == nativeClockID,
                  nativeTiming.nativeIncarnation == authority.nativeIncarnation,
                  nativeTiming.captureStartMs >= 0,
                  nativeTiming.captureEndMs >= nativeTiming.captureStartMs else {
                throw BridgeFailure.invalidResponse
            }
        }
        frameCounter += 1
        let payload = FramePayload(
            imageBase64: imageBase64,
            mime: "image/jpeg",
            width: width,
            height: height,
            logicalWidth: logicalWidth,
            logicalHeight: logicalHeight,
            orientation: orientation,
            capturedAt: capturedAt,
            nativeFrameId: frameCounter,
            nativeTiming: authority.enabled ? nativeTiming : nil
        )
        _ = try post(path: "frame", payload: payload)
    }

    func ack(id: String, ok: Bool, error: String?, timing: String, authority: AuthorityGrant?,
             cleanupEvidence: CleanupTerminationEvidence?) throws {
        _ = try post(path: "ack", payload: AckPayload(
            id: id, ok: ok, error: error, timing: timing, authority: authority,
            cleanupEvidence: cleanupEvidence))
    }

    func shutdown() {}

    private func post<T: Encodable>(path: String, payload: T) throws -> Data {
        let body: Data
        do {
            body = try encoder.encode(payload)
        } catch {
            throw BridgeFailure.invalidResponse
        }
        return try request(method: "POST", path: path, body: body)
    }

    private func request(method: String, path: String, body: Data?) throws -> Data {
        let requestURL = baseURL.appendingPathComponent(path)
        var request = URLRequest(url: requestURL, timeoutInterval: requestTimeout)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if body != nil {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        request.httpBody = body

        let semaphore = DispatchSemaphore(value: 0)
        var responseData: Data?
        var responseError: Error?
        let task = session.dataTask(with: request) { data, response, error in
            responseData = data
            responseError = error
            if let httpResponse = response as? HTTPURLResponse,
               !(200..<300).contains(httpResponse.statusCode) {
                responseError = BridgeFailure.serverReturnedError
            }
            semaphore.signal()
        }
        task.resume()

        guard semaphore.wait(timeout: .now() + requestTimeout) == .success else {
            task.cancel()
            throw BridgeFailure.timeout
        }
        if let responseError {
            if responseError is BridgeFailure {
                throw responseError
            }
            throw BridgeFailure.requestFailed
        }
        guard let responseData else {
            throw BridgeFailure.requestFailed
        }
        return responseData
    }
}

private struct NativeFrameBuffer {
    static let maxFrames = 16
    static let maxBytes = 12 * 1024 * 1024

    struct Frame {
        let id: Int64
        let body: Data
    }

    enum ReadError: Error {
        case cursorAhead
    }

    private var frames: [Frame] = []
    private var bytes = 0

    mutating func append(id: Int64, body: Data) -> Bool {
        guard id > 0, !body.isEmpty, body.count <= Self.maxBytes,
              frames.last.map({ id > $0.id }) ?? true else { return false }
        while !frames.isEmpty &&
                (frames.count >= Self.maxFrames || bytes + body.count > Self.maxBytes) {
            bytes -= frames.removeFirst().body.count
        }
        frames.append(Frame(id: id, body: body))
        bytes += body.count
        return true
    }

    func latest() -> Frame? {
        frames.last
    }

    func after(_ cursor: Int64) throws -> Frame? {
        guard let newest = frames.last?.id else { return nil }
        guard cursor <= newest else { throw ReadError.cursorAhead }
        return frames.first(where: { $0.id > cursor }) ?? frames.last
    }
}

/// Local HTTP bridge for a paired physical device. It is enabled only when the
/// exact CoreDevice tunnel host, port, and bearer token are supplied by the host.
private final class NativeHTTPBridge: LiveBridge {
    private static let maxClients = 8
    private static let maxHeaderBytes = 16 * 1024
    private static let maxBodyBytes = 8 * 1024
    private static let maxFrameBytes = 3 * 1024 * 1024
    private static let maxFrameHeaderBytes = 4096
    private static let idleTimeout: TimeInterval = 5
    private static let maxAckCount = 64

    private struct HTTPRequest {
        let method: String
        let path: String
        let headers: [String: String]
        let body: Data
    }

    private enum ParseResult {
        case incomplete
        case invalid(String)
        case request(HTTPRequest)
    }

    private struct AckReceipt {
        let id: String
        let ok: Bool
        let error: String?
        let timing: String
        let authority: AuthorityGrant?
        let cleanupEvidence: CleanupTerminationEvidence?
    }

    private struct StopEnvelope: Decodable {
        let authority: AuthorityGrant
        private enum CodingKeys: String, CodingKey, CaseIterable { case authority }
        init(from decoder: Decoder) throws {
            try exactKeys(decoder, CodingKeys.self)
            let container = try decoder.container(keyedBy: CodingKeys.self)
            authority = try container.decode(AuthorityGrant.self, forKey: .authority)
        }
    }

    private let listener: NWListener
    private let expectedTokenDigest: Data
    private let networkQueue = DispatchQueue(label: "repro.native-http.network", qos: .userInitiated)
    private let state = NSCondition()
    private var readyFlag = false
    private var activated = false
    private var retirementRequested = false
    private var stopped = false
    private var serverFailed = false
    private var capabilities: [String: Any] = [:]
    private var latestFrame: Data?
    private var frameBuffer = NativeFrameBuffer()
    private var frameCounter: Int64 = 0
    private var queuedCommand: LiveCommand?
    private var inFlightCommandID: String?
    private var inFlightCommandAction: String?
    private var inFlightCommandAuthority: AuthorityGrant?
    private var acceptedDigests: [String: Data] = [:]
    private var acceptedOrder: [String] = []
    private var acknowledgements: [String: AckReceipt] = [:]
    private var acknowledgementOrder: [String] = []
    private var connections: [ObjectIdentifier: NWConnection] = [:]
    private var authoritySequence = 0
    private var lastAuthorityOperationID: String?
    private var startupAuthority: AuthorityGrant?

    init?(environment: [String: String]) {
        guard let host = environment["REPRO_LIVE_LISTEN_HOST"],
              let portString = environment["REPRO_LIVE_LISTEN_PORT"],
              let portNumber = UInt16(portString),
              portNumber > 0,
              Self.isExactIPAddress(host),
              let token = environment["REPRO_LIVE_TOKEN"],
              !token.isEmpty,
              token.count <= 512,
              !token.contains(where: { $0 == "\n" || $0 == "\r" }) else {
            return nil
        }

        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(
            host: NWEndpoint.Host(host),
            port: NWEndpoint.Port(rawValue: portNumber)!
        )
        guard let listener = try? NWListener(using: parameters) else { return nil }
        self.listener = listener
        self.expectedTokenDigest = Data(SHA256.hash(data: Data(token.utf8)))

        listener.stateUpdateHandler = { [weak self] update in
            if case .failed = update {
                self?.state.lock()
                self?.serverFailed = true
                self?.state.broadcast()
                self?.state.unlock()
            }
        }
        listener.newConnectionHandler = { [weak self] connection in
            self?.accept(connection)
        }
        listener.start(queue: networkQueue)
    }

    func next() throws -> NextEnvelope {
        state.lock()
        defer { state.unlock() }
        if serverFailed { throw BridgeFailure.requestFailed }
        if stopped { return NextEnvelope(command: nil, stop: true) }
        guard let command = queuedCommand else {
            return NextEnvelope(command: nil, stop: false)
        }
        queuedCommand = nil
        inFlightCommandID = command.id
        inFlightCommandAction = command.action
        inFlightCommandAuthority = command.authority
        return NextEnvelope(command: command, stop: false)
    }

    func ready() throws -> AuthorityGrant? {
        state.lock()
        let actions = generalRuntimeConfiguration()?.actions ?? legacyLiveActions
        capabilities = [
            "actions": actions,
            "inputMode": "gesture-batch",
            "media": "sampled-jpeg",
            "nativeFrameBufferVersion": 1
        ]
        if NativeAuthorityContext.shared.enabled {
            capabilities["nativeFrameTimingVersion"] = 1
        }
        if !NativeAuthorityContext.shared.enabled { readyFlag = true }
        let failed = serverFailed
        state.broadcast()
        if NativeAuthorityContext.shared.enabled {
            let deadline = Date().addingTimeInterval(90)
            while !activated && !serverFailed && !stopped && Date() < deadline {
                _ = state.wait(until: min(deadline, Date().addingTimeInterval(0.25)))
            }
        }
        let activationFailed = serverFailed || (NativeAuthorityContext.shared.enabled && !activated)
        let grant = startupAuthority
        state.unlock()
        if failed || activationFailed { throw BridgeFailure.requestFailed }
        if NativeAuthorityContext.shared.enabled && grant == nil {
            throw BridgeFailure.invalidResponse
        }
        return grant
    }

    func started(authority grant: AuthorityGrant?) throws {
        let context = NativeAuthorityContext.shared
        if context.enabled {
            guard let grant, grant.matches(context, requireLive: true) else {
                throw BridgeFailure.invalidResponse
            }
            state.lock()
            let valid = activated && grant.operationID == startupAuthority?.operationID &&
                grant.operationFingerprint == startupAuthority?.operationFingerprint &&
                grant.sequence == startupAuthority?.sequence
            if valid { readyFlag = true }
            state.broadcast()
            state.unlock()
            if !valid { throw BridgeFailure.invalidResponse }
        } else {
            state.lock(); readyFlag = true; state.broadcast(); state.unlock()
        }
    }

    func synchronizeClock() throws {}

    func frame(
        imageBase64: String,
        width: Int,
        height: Int,
        logicalWidth: Int,
        logicalHeight: Int,
        orientation: String,
        capturedAt: Int64,
        nativeTiming: NativeFrameTiming?
    ) throws {
        guard imageBase64.count <= 6 * 1024 * 1024,
              let imageData = Data(base64Encoded: imageBase64),
              !imageData.isEmpty,
              imageData.count <= Self.maxFrameBytes,
              width > 0, width <= 10_000,
              height > 0, height <= 10_000 else {
            throw BridgeFailure.captureFailed
        }
        let authority = NativeAuthorityContext.shared
        if authority.enabled {
            guard let nativeTiming,
                  nativeTiming.version == 1,
                  nativeTiming.nativeClockID == nativeClockID,
                  nativeTiming.nativeIncarnation == authority.nativeIncarnation,
                  nativeTiming.captureStartMs >= 0,
                  nativeTiming.captureEndMs >= nativeTiming.captureStartMs else {
                throw BridgeFailure.invalidResponse
            }
        }
        state.lock()
        frameCounter += 1
        let nativeFrameID = frameCounter
        state.unlock()
        var payload: [String: Any] = [
            "id": "native-\(nativeFrameID)",
            "nativeFrameId": nativeFrameID,
            "imageBase64": imageBase64,
            "mime": "image/jpeg",
            "width": width,
            "height": height,
            "logicalWidth": logicalWidth,
            "logicalHeight": logicalHeight,
            "orientation": orientation,
            "capturedAt": capturedAt
        ]
        if authority.enabled, let nativeTiming {
            payload["nativeTiming"] = nativeTiming.wireObject
        }
        guard JSONSerialization.isValidJSONObject(payload),
              let encoded = try? JSONSerialization.data(withJSONObject: payload, options: []),
              encoded.count <= Self.maxFrameHeaderBytes + imageBase64.count else {
            throw BridgeFailure.captureFailed
        }
        state.lock()
        guard frameBuffer.append(id: nativeFrameID, body: encoded) else {
            state.unlock()
            throw BridgeFailure.captureFailed
        }
        latestFrame = encoded
        state.unlock()
    }

    func ack(id: String, ok: Bool, error: String?, timing: String, authority: AuthorityGrant?,
             cleanupEvidence: CleanupTerminationEvidence?) throws {
        guard id.count <= 256, !id.isEmpty else { throw BridgeFailure.invalidResponse }
        state.lock()
        let commandAction = inFlightCommandAction
        state.unlock()
        let authorityContext = NativeAuthorityContext.shared
        if authorityContext.enabled {
            guard let authority,
                  authority.operationID == id,
                  authority.matches(authorityContext, requireLive: false),
                  let expectedAuthority = inFlightCommandAuthority,
                  authority.protocolVersion == expectedAuthority.protocolVersion,
                  authority.operationFingerprint == expectedAuthority.operationFingerprint,
                  authority.payloadDigest == expectedAuthority.payloadDigest,
                  authority.projectID == expectedAuthority.projectID,
                  authority.sessionID == expectedAuthority.sessionID,
                  authority.controllerID == expectedAuthority.controllerID,
                  authority.sequence == expectedAuthority.sequence,
                  authority.ownershipGeneration == expectedAuthority.ownershipGeneration,
                  authority.hostIncarnation == expectedAuthority.hostIncarnation,
                  authority.helperIncarnation == expectedAuthority.helperIncarnation,
                  authority.providerIncarnation == expectedAuthority.providerIncarnation,
                  authority.nativeIncarnation == expectedAuthority.nativeIncarnation,
                  authority.nativeClockID == expectedAuthority.nativeClockID,
                  authority.nativeDeadlineMS == expectedAuthority.nativeDeadlineMS else {
                throw BridgeFailure.invalidResponse
            }
        } else if authority != nil {
            throw BridgeFailure.invalidResponse
        }
        if cleanupEvidence != nil && commandAction != "authority_cleanup" {
            throw BridgeFailure.invalidResponse
        }
        if cleanupEvidence != nil && !ok {
            throw BridgeFailure.invalidResponse
        }
        if commandAction == "authority_cleanup" && ok && cleanupEvidence == nil {
            throw BridgeFailure.invalidResponse
        }
        if let cleanupEvidence {
            let expectedBundle = ProcessInfo.processInfo.environment["REPRO_TARGET_BUNDLE"]
                ?? "io.reproloop.sample.ios"
            guard cleanupEvidence.bundleID == expectedBundle,
                  cleanupEvidence.state == "not-running",
                  cleanupEvidence.observer == "xctest-application-state" else {
                throw BridgeFailure.invalidResponse
            }
        }
        let safeError = error.map { String($0.prefix(64)) }
        let receipt = AckReceipt(id: id, ok: ok, error: safeError,
                                 timing: String(timing.prefix(32)), authority: authority,
                                 cleanupEvidence: cleanupEvidence)
        state.lock()
        acknowledgements[id] = receipt
        acknowledgementOrder.removeAll { $0 == id }
        acknowledgementOrder.append(id)
        while acknowledgementOrder.count > Self.maxAckCount {
            let oldID = acknowledgementOrder.removeFirst()
            acknowledgements.removeValue(forKey: oldID)
        }
        if inFlightCommandID == id {
            inFlightCommandID = nil
            inFlightCommandAction = nil
            inFlightCommandAuthority = nil
        }
        if commandAction == "authority_retire" { stopped = true }
        state.broadcast()
        state.unlock()
    }

    func shutdown() {
        listener.cancel()
        state.lock()
        stopped = true
        state.broadcast()
        let activeConnections = Array(connections.values)
        connections.removeAll()
        state.unlock()
        activeConnections.forEach { $0.cancel() }
    }

    private func accept(_ connection: NWConnection) {
        state.lock()
        guard connections.count < Self.maxClients, !stopped else {
            state.unlock()
            connection.cancel()
            return
        }
        connections[ObjectIdentifier(connection)] = connection
        state.unlock()
        connection.stateUpdateHandler = { [weak self, weak connection] update in
            if case .failed = update { self?.finish(connection) }
            if case .cancelled = update { self?.finish(connection) }
        }
        connection.start(queue: networkQueue)
        receive(connection, buffer: Data())
    }

    private func finish(_ connection: NWConnection?) {
        guard let connection else { return }
        state.lock()
        connections.removeValue(forKey: ObjectIdentifier(connection))
        state.unlock()
    }

    private func receive(_ connection: NWConnection, buffer: Data) {
        let timeout = DispatchWorkItem { [weak connection] in connection?.cancel() }
        networkQueue.asyncAfter(deadline: .now() + Self.idleTimeout, execute: timeout)
        connection.receive(minimumIncompleteLength: 1, maximumLength: Self.maxHeaderBytes + Self.maxBodyBytes + 4) { [weak self, weak connection] data, _, isComplete, error in
            timeout.cancel()
            guard let self, let connection, error == nil, (!isComplete || data != nil) else {
                connection?.cancel()
                return
            }
            var combined = buffer
            if let data { combined.append(data) }
            switch Self.parse(combined) {
            case .incomplete:
                if combined.count > Self.maxHeaderBytes + Self.maxBodyBytes + 4 {
                    self.respond(connection, status: 400, body: self.errorBody("request_too_large"))
                } else {
                    self.receive(connection, buffer: combined)
                }
            case let .invalid(code):
                self.respond(connection, status: 400, body: self.errorBody(code))
            case let .request(request):
                self.authenticate(request, connection: connection)
            }
        }
    }

    private func authenticate(_ request: HTTPRequest, connection: NWConnection) {
        guard let value = request.headers["authorization"], value.hasPrefix("Bearer ") else {
            respond(connection, status: 401, body: errorBody("unauthorized")); return
        }
        let provided = Data(SHA256.hash(data: Data(value.dropFirst(7).utf8)))
        guard Self.constantTimeEqual(provided, expectedTokenDigest) else {
            respond(connection, status: 401, body: errorBody("unauthorized")); return
        }
        route(request, connection: connection)
    }

    private func route(_ request: HTTPRequest, connection: NWConnection) {
        switch (request.method, request.path) {
        case ("GET", "/status"):
            respond(connection, status: 200, body: statusBody())
        case ("GET", "/frame"):
            state.lock(); let frame = latestFrame; state.unlock()
            respond(connection, status: frame == nil ? 404 : 200, body: frame ?? errorBody("frame_unavailable"))
        case ("GET", let path) where path.hasPrefix("/frames/after/"):
            let rawCursor = String(path.dropFirst("/frames/after/".count))
            guard !rawCursor.isEmpty,
                  rawCursor.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
                  let cursor = Int64(rawCursor) else {
                respond(connection, status: 400, body: errorBody("invalid_frame_cursor"))
                return
            }
            state.lock()
            do {
                let frame = try frameBuffer.after(cursor)
                state.unlock()
                respond(connection, status: frame == nil ? 404 : 200,
                        body: frame?.body ?? errorBody("frame_unavailable"))
            } catch NativeFrameBuffer.ReadError.cursorAhead {
                state.unlock()
                respond(connection, status: 409, body: errorBody("frame_cursor_ahead"))
            } catch {
                state.unlock()
                respond(connection, status: 409, body: errorBody("frame_cursor_ahead"))
            }
        case ("POST", "/command"):
            guard let data = submitCommand(request.body) else {
                let status = commandConflict(request.body) ? 409 : 400
                respond(connection, status: status, body: errorBody(status == 409 ? "duplicate_command" : "invalid_command"))
                return
            }
            respond(connection, status: 202, body: data)
        case ("POST", "/retire"):
            guard let data = submitRetirement(request.body) else {
                respond(connection, status: 409, body: errorBody("authority_rejected")); return
            }
            respond(connection, status: 202, body: data)
        case ("POST", "/activate"):
            guard NativeAuthorityContext.shared.enabled,
                  strictAuthorityNumberTypes(request.body),
                  let envelope = try? JSONDecoder().decode(StopEnvelope.self, from: request.body),
                  envelope.authority.matches(NativeAuthorityContext.shared, requireLive: true),
                  envelope.authority.sequence > authoritySequence,
                  !retirementRequested, !stopped else {
                respond(connection, status: 409, body: errorBody("authority_rejected")); return
            }
            state.lock()
            authoritySequence = envelope.authority.sequence
            lastAuthorityOperationID = envelope.authority.operationID
            startupAuthority = envelope.authority
            activated = true
            state.broadcast()
            state.unlock()
            let response: [String: Any] = ["activated": true, "authority": envelope.authority.wireObject]
            respond(connection, status: 200,
                    body: (try? JSONSerialization.data(withJSONObject: response)) ?? errorBody("activate_failed"))
        case ("GET", let path) where path.hasPrefix("/ack/"):
            let id = String(path.dropFirst(5))
            respond(connection, status: id.isEmpty ? 400 : 200, body: ackBody(id))
        case ("POST", "/stop"):
            var stopAuthority: AuthorityGrant?
            if NativeAuthorityContext.shared.enabled {
                guard strictAuthorityNumberTypes(request.body),
                      let envelope = try? JSONDecoder().decode(StopEnvelope.self, from: request.body),
                      envelope.authority.matches(NativeAuthorityContext.shared, requireLive: true),
                      envelope.authority.operationID == lastAuthorityOperationID else {
                    respond(connection, status: 409, body: errorBody("authority_rejected")); return
                }
                stopAuthority = envelope.authority
            } else if !(request.body.isEmpty || request.body == Data("{}".utf8)) {
                respond(connection, status: 400, body: errorBody("invalid_stop")); return
            }
            state.lock(); stopped = true; state.broadcast(); state.unlock()
            var response: [String: Any] = ["stopped": true]
            if let stopAuthority { response["authority"] = stopAuthority.wireObject }
            respond(connection, status: 200,
                    body: (try? JSONSerialization.data(withJSONObject: response)) ?? errorBody("stop_failed"))
        default:
            respond(connection, status: 404, body: errorBody("not_found"))
        }
    }

    private func submitCommand(_ body: Data) -> Data? {
        guard strictAuthorityNumberTypes(body),
              let command = try? JSONDecoder().decode(LiveCommand.self, from: body),
              command.id.count <= 256, !command.id.isEmpty,
              command.action.count <= 64, !command.action.isEmpty else { return nil }
        let digest = Data(SHA256.hash(data: body))
        state.lock()
        defer { state.unlock() }
        if let accepted = acceptedDigests[command.id] {
            return Self.constantTimeEqual(accepted, digest) ? Data("{\"accepted\":true}".utf8) : nil
        }
        guard queuedCommand == nil, inFlightCommandID == nil, !stopped,
              !retirementRequested, command.action != "authority_retire" else { return nil }
        let authority = NativeAuthorityContext.shared
        if authority.enabled {
            guard let grant = command.authority, grant.operationID == command.id,
                  grant.matches(authority, requireLive: true), grant.sequence > authoritySequence else { return nil }
            authoritySequence = grant.sequence
            lastAuthorityOperationID = grant.operationID
        } else if command.authority != nil { return nil }
        acceptedDigests[command.id] = digest
        acceptedOrder.append(command.id)
        while acceptedOrder.count > Self.maxAckCount {
            acceptedDigests.removeValue(forKey: acceptedOrder.removeFirst())
        }
        queuedCommand = command
        state.signal()
        return Data("{\"accepted\":true}".utf8)
    }

    private func submitRetirement(_ body: Data) -> Data? {
        guard strictAuthorityNumberTypes(body),
              let command = try? JSONDecoder().decode(LiveCommand.self, from: body),
              command.action == "authority_retire", command.payload.isEmpty,
              let grant = command.authority, grant.operationID == command.id,
              grant.payloadDigest == "e400a107b18485b912d245949006f3585967c3098bc7c74435a3d1eff87e3dc9",
              grant.matches(NativeAuthorityContext.shared, requireLive: true) else { return nil }
        let digest = Data(SHA256.hash(data: body))
        state.lock()
        defer { state.unlock() }
        if let accepted = acceptedDigests[command.id] {
            return Self.constantTimeEqual(accepted, digest) ? Data("{\"accepted\":true}".utf8) : nil
        }
        guard !stopped, grant.sequence > authoritySequence else { return nil }
        retirementRequested = true
        authoritySequence = grant.sequence
        lastAuthorityOperationID = grant.operationID
        acceptedDigests[command.id] = digest
        // Discard queued ordinary input. An in-flight gesture finishes before
        // the fixed retirement command, after which no launch/input is admitted.
        queuedCommand = activated ? command : nil
        if !activated { stopped = true }
        state.broadcast()
        return Data("{\"accepted\":true}".utf8)
    }

    private func commandConflict(_ body: Data) -> Bool {
        guard let command = try? JSONDecoder().decode(LiveCommand.self, from: body) else { return false }
        state.lock(); defer { state.unlock() }
        return acceptedDigests[command.id] != nil || queuedCommand != nil || inFlightCommandID != nil
    }

    /// helper가 도달 가능한 비루프백 주소를 보고한다. 호스트는 이 목록으로
    /// 터널 주소 이외의 경로에서 helper 포트가 닫혀 있는지 측정한다.
    private static func nonLoopbackInterfaceAddresses() -> [String] {
        var result: [String] = []
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return [] }
        defer { freeifaddrs(first) }
        var current: UnsafeMutablePointer<ifaddrs>? = first
        while let item = current {
            defer { current = item.pointee.ifa_next }
            let flags = Int32(item.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_LOOPBACK == 0,
                  let socket = item.pointee.ifa_addr else { continue }
            let family = Int32(socket.pointee.sa_family)
            guard family == AF_INET || family == AF_INET6 else { continue }
            var buffer = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            guard getnameinfo(socket, socklen_t(socket.pointee.sa_len), &buffer,
                              socklen_t(buffer.count), nil, 0, NI_NUMERICHOST) == 0 else { continue }
            result.append(String(cString: buffer))
        }
        return result.sorted()
    }

    private func statusBody() -> Data {
        state.lock()
        var value: [String: Any] = ["ready": readyFlag, "stopped": stopped, "capabilities": capabilities,
                                    "networkInterfaces": Self.nonLoopbackInterfaceAddresses()]
        if NativeAuthorityContext.shared.enabled {
            value["retirementVersion"] = 1
            value["authoritySequence"] = authoritySequence
        }
        if let runtime = generalRuntimeConfiguration() {
            value["targetBundle"] = ProcessInfo.processInfo.environment["REPRO_TARGET_BUNDLE"]
            value["applicationProfileDigest"] = runtime.profileDigest
        }
        let authority = NativeAuthorityContext.shared
        if authority.enabled {
            value.updateValue(nativeProtocolVersion, forKey: "protocolVersion")
            value.updateValue(nativeHelperVersion, forKey: "helperVersion")
            value.updateValue(authority.helperIncarnation!, forKey: "helperIncarnation")
            value.updateValue(authority.hostIncarnation!, forKey: "hostIncarnation")
            value.updateValue(authority.providerIncarnation!, forKey: "providerIncarnation")
            value.updateValue(authority.nativeIncarnation, forKey: "nativeIncarnation")
            value.updateValue(nativeClockID, forKey: "nativeClockId")
            value.updateValue(nativeContinuousTimeMS(), forKey: "nativeTimeMs")
        }
        state.unlock()
        return (try? JSONSerialization.data(withJSONObject: value, options: [])) ?? errorBody("status_failed")
    }

    private func ackBody(_ id: String) -> Data {
        state.lock()
        if let receipt = acknowledgements[id] {
            var value: [String: Any] = ["pending": false, "id": receipt.id, "ok": receipt.ok, "timing": receipt.timing]
            if let error = receipt.error { value["error"] = error }
            if let authority = receipt.authority { value["authority"] = authority.wireObject }
            if let cleanupEvidence = receipt.cleanupEvidence {
                value["cleanupEvidence"] = cleanupEvidence.wireObject
            }
            state.unlock()
            return (try? JSONSerialization.data(withJSONObject: value, options: [])) ?? errorBody("ack_failed")
        }
        state.unlock()
        return Data("{\"pending\":true}".utf8)
    }

    private func respond(_ connection: NWConnection, status: Int, body: Data) {
        let reason = Self.reason(status)
        var response = Data("HTTP/1.1 \(status) \(reason)\r\nContent-Type: application/json\r\nContent-Length: \(body.count)\r\nConnection: close\r\n\r\n".utf8)
        response.append(body)
        connection.send(content: response, completion: .contentProcessed { [weak self, weak connection] _ in
            connection?.cancel()
            self?.finish(connection)
        })
    }

    private func errorBody(_ code: String) -> Data {
        Data("{\"error\":\"\(code)\"}".utf8)
    }

    private static func reason(_ status: Int) -> String {
        switch status {
        case 200: return "OK"
        case 202: return "Accepted"
        case 400: return "Bad Request"
        case 401: return "Unauthorized"
        case 404: return "Not Found"
        case 409: return "Conflict"
        default: return "Server Error"
        }
    }

    private static func parse(_ data: Data) -> ParseResult {
        guard let headerEnd = data.range(of: Data("\r\n\r\n".utf8)) else {
            return data.count > maxHeaderBytes ? .invalid("headers_too_large") : .incomplete
        }
        guard headerEnd.lowerBound <= maxHeaderBytes,
              let header = String(data: data[..<headerEnd.lowerBound], encoding: .utf8) else {
            return .invalid("invalid_headers")
        }
        let lines = header.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { return .invalid("invalid_request") }
        let parts = requestLine.split(separator: " ", omittingEmptySubsequences: true)
        guard parts.count == 3, parts[2] == "HTTP/1.1" else { return .invalid("invalid_request") }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            let pieces = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
            guard pieces.count == 2 else { return .invalid("invalid_headers") }
            let name = pieces[0].trimmingCharacters(in: .whitespaces).lowercased()
            let value = pieces[1].trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty, headers[name] == nil else { return .invalid("invalid_headers") }
            headers[name] = value
        }
        if headers["transfer-encoding"] != nil { return .invalid("chunked_not_supported") }
        let method = String(parts[0])
        guard method == "GET" || method == "POST" else { return .invalid("method_not_allowed") }
        let contentLength: Int
        if let rawLength = headers["content-length"] {
            guard let length = Int(rawLength), length >= 0, length <= maxBodyBytes else { return .invalid("body_too_large") }
            contentLength = length
        } else {
            contentLength = 0
        }
        if method == "POST" && headers["content-length"] == nil { return .invalid("content_length_required") }
        if method == "GET" && contentLength != 0 { return .invalid("body_not_allowed") }
        let bodyStart = headerEnd.upperBound
        guard data.count >= bodyStart else { return .incomplete }
        guard data.count - bodyStart >= contentLength else { return .incomplete }
        let body = data[bodyStart..<(bodyStart + contentLength)]
        return .request(HTTPRequest(method: method, path: String(parts[1]), headers: headers, body: Data(body)))
    }

    private static func constantTimeEqual(_ left: Data, _ right: Data) -> Bool {
        guard left.count == right.count else { return false }
        var difference: UInt8 = 0
        for (a, b) in zip(left, right) { difference |= a ^ b }
        return difference == 0
    }

    private static func isExactIPAddress(_ raw: String) -> Bool {
        guard !raw.isEmpty, !raw.contains("["), !raw.contains("]") else { return false }
        let host = raw.split(separator: "%", maxSplits: 1, omittingEmptySubsequences: true).first.map(String.init) ?? raw
        var v4 = in_addr()
        if host.withCString({ inet_pton(AF_INET, $0, &v4) }) == 1 {
            return v4.s_addr != 0
        }
        var v6 = in6_addr()
        guard host.withCString({ inet_pton(AF_INET6, $0, &v6) }) == 1 else { return false }
        let bytes = withUnsafeBytes(of: &v6) { Array($0) }
        return bytes.contains { $0 != 0 }
    }
}
