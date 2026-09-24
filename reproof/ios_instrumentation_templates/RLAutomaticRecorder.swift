#if DEBUG || REPRO_OBSERVATIONS
import Foundation
import UIKit
import CoreFoundation
import CryptoKit

@objc protocol RLSanitationRuntimeBridge {
    static func currentLaunchReceipt() -> NSDictionary?
    static func completeValidatedStartup(bundleID: String, buildID: String,
                                         profileDigest: String, startedAtMs: Int) -> Bool
}

/// Each event or status change can start one bounded storage retry burst.
struct RLAppLogRetryPolicy {
    private var revision = 0
    private var attemptedRevision = 0
    private(set) var failures = 0
    mutating func changed() { revision += 1 }
    mutating func canSchedule() -> Bool {
        if failures >= 3 {
            guard revision > attemptedRevision else { return false }
            failures = 0
        }
        return true
    }
    mutating func beginAttempt() { attemptedRevision = revision }
    mutating func finished(success: Bool) { failures = success ? 0 : min(3, failures + 1) }
}

/// The automatic UIKit recorder is intentionally a closed, debug-only adapter.
/// `RLAutoConfig` is emitted by the build preparer in the same application
/// target. The legacy replay profile and the configured observation profile
/// are separate contracts; neither interprets policies supplied by the host.
@objc(RLAutomaticRecorder)
public final class RLAutomaticRecorder: NSObject {
    private static let supportedApplicationID = "io.reproloop.sample.ios"
    private static let supportedProject = "ReproLoop.xcodeproj"
    private static let supportedTarget = "ReproSample"
    private static let supportedKind = "uikit-runtime-v1"
    private static let maxActions = 500
    private static let maxCaptureBytes = 20 * 1024 * 1024
    private static let maxDiagnosticsBytes = 1024 * 1024
    private static let maxDurationMs = 10 * 60 * 1000
    private static let maxViewNodes = 512
    private static let maxViewDepth = 32
    private static let maxAppLogEvents = 2_000
    private static let maxAppLogBytes = 1024 * 1024
    private static let maxAppLogDurationMs = 1_800_000
    private static let appLogEventReserveBytes = 512

    private struct Profile {
        let observationOnly: Bool
        let applicationID: String
        let project: String
        let target: String
        let cases: Set<String>
        let textTargets: [String]
        let numericTargets: [String]
        let tapTargets: Set<String>
        let backTarget: String
        let screenTargets: [String: String]
        let startScreen: String
        let startNodes: [String: String]
    }

    private struct Event {
        let id: String
        let seq: Int
        let action: String
        let target: String
        let parameters: [String: String]
        let elapsedMs: Int

        func object() -> [String: Any] {
            [
                "id": id,
                "seq": seq,
                "action": action,
                "target": target,
                "parameters": parameters,
                "elapsedMs": elapsedMs
            ]
        }
    }

    private struct AppLogEvent {
        let seq: Int
        let elapsedMs: Int
        let type: String
        let name: String
        let component: String
        let componentID: String
        let target: String?

        func object() -> [String: Any] {
            [
                "seq": seq,
                "elapsedMs": elapsedMs,
                "type": type,
                "name": name,
                "component": component,
                "componentId": componentID,
                "target": target ?? NSNull()
            ]
        }
    }

    private struct AppLogActionContext {
        let sequence: Int
        let target: String
        let componentID: String
    }

    private struct ObservedScreen {
        let name: String
        let componentID: String
    }

    fileprivate struct Snapshot {
        let screen: String
        let text: [String: String]
        let numeric: [String: String]
    }

    @objc(RLAutoActionToken)
    public final class ActionToken: NSObject {
        fileprivate let replayEventID: String?
        fileprivate let replayTarget: String?
        fileprivate let replayBefore: Snapshot?
        fileprivate let appLogSequence: Int?
        fileprivate let appLogTarget: String?
        fileprivate let appLogComponentID: String?

        fileprivate init(replayEventID: String?,
                         replayTarget: String?,
                         replayBefore: Snapshot?,
                         appLogSequence: Int?,
                         appLogTarget: String?,
                         appLogComponentID: String?) {
            self.replayEventID = replayEventID
            self.replayTarget = replayTarget
            self.replayBefore = replayBefore
            self.appLogSequence = appLogSequence
            self.appLogTarget = appLogTarget
            self.appLogComponentID = appLogComponentID
        }
    }

    private static let shared = RLAutomaticRecorder()

    private let ioQueue = DispatchQueue(label: "io.reproloop.auto-recorder.io", qos: .utility)
    private let appLogIOQueue = DispatchQueue(label: "io.reproloop.app-log.io", qos: .utility)
    private var profile: Profile?
    private var applicationID = ""
    private var runID = ""
    private var caseID = ""
    private var sessionID = ""
    private var profileDigest = ""
    private var buildID = ""
    private var startedAtMs = 0
    private var startUptime: TimeInterval = 0
    private var sessionDirectory: URL?
    private var baseDirectory: URL?
    private var lastCommittedText: [String: String] = [:]
    private var events: [Event] = []
    private var diagnostics: [[String: Any]] = []
    private var sequence = 0
    private var eventBytes = 0
    private var captureInvalid = false
    private var lifecycleInterrupted = false
    private var started = false
    private var starting = false
    private var finishing = false
    private var finished = false
    private var captureEnabled = false
    private var startupMarkerData: Data?
    private var requestObserverInstalled = false
    private var lifecycleObservers: [NSObjectProtocol] = []
    private var appLogEnabled = false
    private var appLogStarted = false
    private var appLogSessionID = ""
    private var appLogStartedAtMs = 0
    private var appLogStartUptime: TimeInterval = 0
    private var appLogDirectory: URL?
    private var appLogEvents: [AppLogEvent] = []
    private var appLogSequence = 0
    private var appLogBytes = 0
    private var appLogTruncated = false
    private var appLogLostEvents = false
    private var appLogObservers: [NSObjectProtocol] = []
    private var appLogScreenTimer: Timer?
    private var observedScreen: ObservedScreen?
    private var appLogSnapshotScheduled = false
    private var appLogSnapshotWriting = false
    private var appLogSnapshotDirty = false
    private var appLogRetry = RLAppLogRetryPolicy()
    private var retryCount = 0
    private var pendingAction: ActionToken?

    private override init() {
        super.init()
    }

    // MARK: - Public Objective-C surface

    /// Called by the Objective-C +load shim. It is safe to call repeatedly.
    @objc public static func bootstrap() -> Bool {
        if Thread.isMainThread {
            return RLAutomaticRecorder.shared.bootstrapOnMain()
        }
        let configured = RLAutomaticRecorder.shared.configurationIsValid()
        DispatchQueue.main.async {
            RLAutomaticRecorder.shared.bootstrapOnMain()
        }
        return configured
    }

    /// A test/host convenience. Production control uses the Darwin request
    /// notification registered during startup; this method never bypasses the
    /// same validation and serialization path.
    @objc public static func freeze() {
        DispatchQueue.main.async {
            RLAutomaticRecorder.shared.finishOnMain()
        }
    }

    /// Called by the action shim immediately before the product action. A nil
    /// result means the product action must still run but is outside the
    /// recorder's bounded capture set.
    @objc(_rlWillSendAction:to:from:forEvent:)
    public static func _rlWillSendAction(_ action: Selector,
                                         to target: Any?,
                                         from sender: Any?,
                                         forEvent event: UIEvent?) -> ActionToken? {
        guard Thread.isMainThread else {
            shared.invalidateCapture()
            return nil
        }
        return shared.willSendActionOnMain(action, target: target, sender: sender, event: event)
    }

    @objc(_rlActionReturned:)
    public static func _rlActionReturned(_ token: ActionToken) {
        guard Thread.isMainThread else {
            shared.invalidateCapture()
            return
        }
        shared.actionReturnedOnMain(token)
    }

    @objc(_rlActionThrew:)
    public static func _rlActionThrew(_ token: ActionToken) {
        guard Thread.isMainThread else {
            shared.invalidateCapture()
            return
        }
        shared.actionThrewOnMain(token)
    }

    @objc(_rlCollectorFailure)
    public static func _rlCollectorFailure() {
        shared.invalidateCapture()
        if Thread.isMainThread { shared.markAppLogLost() }
    }

    @objc(_rlAppLogFailure)
    public static func _rlAppLogFailure() {
        guard Thread.isMainThread else { return }
        shared.markAppLogLost()
    }

    @objc(_rlViewControllerAppearing:)
    public static func _rlViewControllerAppearing(_ controller: AnyObject) {
        guard Thread.isMainThread else { return }
        shared.appendAppLogViewControllerEvent("appeared", controller: controller)
    }

    @objc(_rlViewControllerDisappearing:)
    public static func _rlViewControllerDisappearing(_ controller: AnyObject) {
        guard Thread.isMainThread else { return }
        shared.appendAppLogViewControllerEvent("disappeared", controller: controller)
    }

    // MARK: - Startup and gating

    @discardableResult
    private func bootstrapOnMain() -> Bool {
        guard Thread.isMainThread else { return false }
        guard !started, !starting, !finishing, !finished else { return false }
        guard validateEnvironment() else { return false }
        guard let profile = loadProfile() else { return false }
        self.profile = profile
        startAppLogOnMain()
        if profile.observationOnly {
            started = true
            publishRuntimeIdentity(startedAtMs: Int(Date().timeIntervalSince1970 * 1000.0))
            return true
        }
        registerLifecycleObservers()
        scheduleStartupAttempt()
        return true
    }

    private func configurationIsValid() -> Bool {
        guard validateEnvironment() else { return false }
        return loadProfile() != nil
    }

    private func validateEnvironment() -> Bool {
        let environment = ProcessInfo.processInfo.environment
        guard let configProfileData = RLAutoConfig.profileJSON.data(using: .utf8),
              configProfileData.count <= 256 * 1024,
              let configObject = try? JSONSerialization.jsonObject(with: configProfileData),
              let configProfile = configObject as? [String: Any],
              let configuredApplicationID = configProfile["applicationId"] as? String else { return false }
        let observationOnly = configProfile["kind"] as? String == "uikit-observation-v2"
        guard environment["REPRO_MODE"] == (observationOnly ? "observe" : "record") else { return false }
        guard let rawDigest = environment["REPRO_AUTO_PROFILE_DIGEST"],
              isDigest(rawDigest),
              isDigest(RLAutoConfig.profileDigest),
              RLAutoConfig.profileDigest == RLAutoConfig.profileDigest.lowercased(),
              rawDigest == RLAutoConfig.profileDigest else { return false }
        guard let rawRunID = environment["REPRO_RUN_ID"],
              let uuid = UUID(uuidString: rawRunID) else { return false }
        let rawCase: String
        if observationOnly {
            guard environment["REPRO_CASE"] == nil, environment["REPRO_FIXTURE_RESET"] == nil else { return false }
            rawCase = ""
        } else {
            guard let selectedCase = environment["REPRO_CASE"],
                  Self.allowedCases.contains(selectedCase),
                  configuredApplicationID == Self.supportedApplicationID else { return false }
            rawCase = selectedCase
        }
        guard Bundle.main.bundleIdentifier == configuredApplicationID else { return false }
        guard let infoDigest = Bundle.main.object(forInfoDictionaryKey: "ReproAutoProfileDigest") as? String,
              infoDigest == RLAutoConfig.profileDigest else { return false }
        guard let embeddedBuildID = Bundle.main.object(forInfoDictionaryKey: "ReproBuildID") as? String,
              isBuildID(embeddedBuildID),
              let infoProfile = Bundle.main.object(forInfoDictionaryKey: "ReproAutoProfile") as? [String: Any],
              NSDictionary(dictionary: infoProfile).isEqual(to: configProfile) else { return false }

        runID = uuid.uuidString.lowercased()
        profileDigest = RLAutoConfig.profileDigest
        caseID = rawCase
        buildID = embeddedBuildID
        guard let runtimeBundleID = Bundle.main.bundleIdentifier else { return false }
        applicationID = runtimeBundleID
        appLogEnabled = isAppLogSchemaEnabled(Bundle.main.object(forInfoDictionaryKey: "ReproAppLogSchemaVersion"))
        if observationOnly && !appLogEnabled { return false }
        return true
    }

    private static let allowedCases: Set<String> = ["counter", "duplicate-submit", "reset"]

    private func loadProfile() -> Profile? {
        guard let data = RLAutoConfig.profileJSON.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data),
              let document = object as? [String: Any] else { return nil }
        if document["kind"] as? String == "uikit-observation-v2" {
            return loadObservationProfile(document)
        }
        guard
              document.keys.count == 12,
              document["schemaVersion"] as? Int == 1,
              document["kind"] as? String == Self.supportedKind,
              document["applicationId"] as? String == Self.supportedApplicationID,
              document["project"] as? String == Self.supportedProject,
              document["target"] as? String == Self.supportedTarget,
              let cases = document["cases"] as? [String],
              cases == ["counter", "duplicate-submit", "reset"],
              let textTargets = document["textTargets"] as? [String],
              textTargets == ["counter.name"],
              let numericTargets = document["numericTargets"] as? [String],
              numericTargets == ["counter.count"],
              let tapTargets = document["tapTargets"] as? [String],
              tapTargets == ["counter.add", "counter.next", "counter.reset"],
              document["backTarget"] as? String == "counter.back",
              let screenTargets = document["screenTargets"] as? [String: String],
              screenTargets == ["counter.screen.main": "main", "counter.screen.details": "details"],
              let startState = document["startState"] as? [String: Any],
              startState.keys.count == 2,
              startState["screen"] as? String == "main",
              let startNodes = startState["nodes"] as? [String: String],
              startNodes == ["counter.name": "", "counter.count": "0"] else {
            return nil
        }

        guard cases.contains(caseID) else { return nil }
        return Profile(observationOnly: false, applicationID: Self.supportedApplicationID,
                       project: Self.supportedProject,
                       target: Self.supportedTarget,
                       cases: Set(cases),
                       textTargets: textTargets,
                       numericTargets: numericTargets,
                       tapTargets: Set(tapTargets),
                       backTarget: "counter.back",
                       screenTargets: screenTargets,
                       startScreen: "main",
                       startNodes: startNodes)
    }

    private func loadObservationProfile(_ document: [String: Any]) -> Profile? {
        func matches(_ value: String, _ pattern: String) -> Bool {
            value.range(of: pattern, options: .regularExpression) != nil
        }
        func identifier(_ value: String) -> Bool {
            matches(value, "^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
        }
        func buildName(_ value: String) -> Bool {
            matches(value, "^[A-Za-z][A-Za-z0-9_. -]{0,99}$") && value == value.trimmingCharacters(in: .whitespaces)
        }
        func inputPath(_ value: String) -> Bool {
            let parts = value.split(separator: "/", omittingEmptySubsequences: false)
            return matches(value, "^[A-Za-z0-9_./ -]{1,512}$")
                && parts.allSatisfy { !$0.isEmpty && !$0.hasPrefix(".") }
        }
        guard Set(document.keys) == Set(["schemaVersion", "kind", "applicationId", "project", "target",
                                         "build", "sourceInputs", "tapTargets", "screenTargets"]),
              let version = document["schemaVersion"] as? NSNumber,
              CFGetTypeID(version) != CFBooleanGetTypeID(), version.doubleValue == 2,
              let applicationID = document["applicationId"] as? String, applicationID.count <= 180,
              matches(applicationID, "^[A-Za-z][A-Za-z0-9-]*(\\.[A-Za-z][A-Za-z0-9-]*)+$"),
              !["io.reproloop.live", "io.reproloop.driver"].contains(applicationID),
              let project = document["project"] as? String, project.hasSuffix(".xcodeproj"), inputPath(project),
              let target = document["target"] as? String, buildName(target),
              let build = document["build"] as? [String: String],
              Set(build.keys) == Set(["scheme", "product", "infoPlist", "debugConfiguration"]),
              let scheme = build["scheme"], buildName(scheme),
              let product = build["product"], buildName(product),
              let configuration = build["debugConfiguration"], buildName(configuration),
              let info = build["infoPlist"], inputPath(info), info.hasSuffix(".plist"),
              let inputs = document["sourceInputs"] as? [String], !inputs.isEmpty, inputs.count <= 900,
              inputs.allSatisfy(inputPath), Set(inputs.map { $0.lowercased() }).count == inputs.count,
              inputs.contains(project + "/project.pbxproj"), inputs.contains(info),
              let taps = document["tapTargets"] as? [String], !taps.isEmpty, taps.count <= 128,
              taps.allSatisfy(identifier), Set(taps).count == taps.count,
              let screens = document["screenTargets"] as? [String: String], !screens.isEmpty, screens.count <= 64,
              screens.keys.allSatisfy(identifier), screens.values.allSatisfy(identifier),
              Set(screens.values).count == screens.count, Set(taps).isDisjoint(with: screens.keys),
              caseID.isEmpty else { return nil }
        return Profile(observationOnly: true, applicationID: applicationID, project: project, target: target,
                       cases: [], textTargets: [], numericTargets: [], tapTargets: Set(taps), backTarget: "",
                       screenTargets: screens, startScreen: "", startNodes: [:])
    }

    private func scheduleStartupAttempt() {
        guard !started, !starting, !finishing, !finished else { return }
        if attemptStartupOnMain() { return }
        guard retryCount < 100 else { return }
        retryCount += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self else { return }
            self.scheduleStartupAttempt()
        }
    }

    @discardableResult
    private func attemptStartupOnMain() -> Bool {
        guard Thread.isMainThread else { return false }
        guard !started, !starting, !finishing, !finished,
              UIApplication.shared.applicationState == .active,
              let profile,
              let window = activeKeyWindow() else { return false }

        starting = true
        guard let snapshot = snapshot(window: window, profile: profile, checkControlShapes: true),
              snapshot.screen == profile.startScreen,
              snapshot.text == Dictionary(uniqueKeysWithValues: profile.startNodes.filter({ profile.textTargets.contains($0.key) })),
              snapshot.numeric == Dictionary(uniqueKeysWithValues: profile.startNodes.filter({ profile.numericTargets.contains($0.key) })) else {
            starting = false
            return false
        }

        let now = Date().timeIntervalSince1970
        let startedMillis = Int(now * 1000.0)
        let session = UUID().uuidString.lowercased()
        let base = applicationSupportDirectory()
        let sessionURL = base.appendingPathComponent(session, isDirectory: true)
        let fixture: [String: Any] = ["id": "ios-\(caseID)", "version": 1, "inputs": [:] as [String: String]]
        let metadata: Data
        let marker: Data
        do {
            metadata = try jsonData(metadataObject(sessionID: session, fixture: fixture,
                                                   startedAtMs: startedMillis, finalized: false, endSequence: nil))
            marker = try jsonData([
                "schemaVersion": 1,
                "runId": runID,
                "sessionId": session,
                "profileDigest": profileDigest,
                "buildId": buildID,
                "fixture": fixture,
                "startedAtMs": startedMillis,
                "finalized": false
            ])
        } catch {
            starting = false
            return false
        }

        sessionID = session
        startedAtMs = startedMillis
        startUptime = ProcessInfo.processInfo.systemUptime
        sessionDirectory = sessionURL
        baseDirectory = base
        lastCommittedText = snapshot.text
        events.removeAll(keepingCapacity: true)
        diagnostics.removeAll(keepingCapacity: true)
        sequence = 0
        eventBytes = 0
        captureInvalid = false
        lifecycleInterrupted = false
        pendingAction = nil
        captureEnabled = false
        startupMarkerData = marker
        ioQueue.async { [weak self] in
            guard let self else { return }
            var success = false
            do {
                try FileManager.default.createDirectory(at: sessionURL, withIntermediateDirectories: true)
                try self.writeDurably(metadata, to: sessionURL.appendingPathComponent("metadata.json"))
                try self.writeDurably(Data(), to: sessionURL.appendingPathComponent("events.jsonl"))
                success = true
            } catch {
                success = false
            }
            DispatchQueue.main.async {
                self.completeStartup(success: success)
            }
        }
        return true
    }

    private func completeStartup(success: Bool) {
        guard Thread.isMainThread else { return }
        guard starting else { return }
        starting = false
        guard success,
              !captureInvalid,
              !lifecycleInterrupted,
              !finished,
              UIApplication.shared.applicationState == .active,
              let profile,
              let window = activeKeyWindow(),
              let snapshot = snapshot(window: window, profile: profile, checkControlShapes: true),
              snapshot.screen == profile.startScreen,
              snapshot.text == Dictionary(uniqueKeysWithValues: profile.startNodes.filter({ profile.textTargets.contains($0.key) })),
              snapshot.numeric == Dictionary(uniqueKeysWithValues: profile.startNodes.filter({ profile.numericTargets.contains($0.key) })) else {
            captureInvalid = true
            abortBeforeReady(removeMarker: false)
            return
        }
        started = true
        // Arm recording before publishing the marker that unlocks host input.
        // A user action can therefore never fall into the marker/ready gap.
        captureEnabled = true
        installDarwinRequestObserver()
        guard let marker = startupMarkerData, let base = baseDirectory else {
            captureInvalid = true
            abortBeforeReady(removeMarker: false)
            return
        }
        startupMarkerData = nil
        let sanitation = Self.currentSanitationReceipt()
        ioQueue.async { [weak self] in
            guard let self else { return }
            var markerWritten = false
            do {
                try ReproRuntimeIdentityWriter.write(bundleID: self.applicationID,
                                                      buildID: self.buildID,
                                                      runID: self.runID,
                                                      profileDigest: self.profileDigest,
                                                      startedAtMs: self.startedAtMs,
                                                      sanitation: sanitation,
                                                      to: base)
                try self.writeDurably(marker, to: base.appendingPathComponent("auto-session.json"))
                markerWritten = true
            } catch {
                markerWritten = false
            }
            DispatchQueue.main.async {
                self.completeReady(markerWritten: markerWritten)
            }
        }
    }

    private func completeReady(markerWritten: Bool) {
        guard Thread.isMainThread else { return }
        // A fast, already accepted freeze owns finalization. Do not remove its
        // marker merely because this startup notification was delivered later.
        if markerWritten && (finishing || finished) { return }
        guard markerWritten,
              started,
              !finished,
              !finishing,
              !captureInvalid,
              !lifecycleInterrupted,
              UIApplication.shared.applicationState == .active,
              activeKeyWindow() != nil else {
            abortBeforeReady(removeMarker: markerWritten)
            return
        }
        guard armSanitationCleanup(startedAtMs: startedAtMs) else {
            abortBeforeReady(removeMarker: true)
            return
        }
        captureEnabled = true
        postDarwin(name: "io.reproloop.auto.ready.\(runID)")
    }

    private func abortBeforeReady(removeMarker: Bool) {
        guard Thread.isMainThread else { return }
        started = false
        captureEnabled = false
        finished = true
        startupMarkerData = nil
        removeDarwinRequestObserver()
        for observer in lifecycleObservers {
            NotificationCenter.default.removeObserver(observer)
        }
        lifecycleObservers.removeAll()
        let runID = self.runID
        let base = baseDirectory
        guard removeMarker, let base else {
            postDarwin(name: "io.reproloop.auto.invalid.\(runID)")
            return
        }
        ioQueue.async { [weak self] in
            try? FileManager.default.removeItem(at: base.appendingPathComponent("auto-session.json"))
            DispatchQueue.main.async {
                self?.postDarwin(name: "io.reproloop.auto.invalid.\(runID)")
            }
        }
    }

    // MARK: - Independent app observation journal

    private func startAppLogOnMain() {
        guard Thread.isMainThread, appLogEnabled, !appLogStarted else { return }
        let session = UUID().uuidString.lowercased()
        let startedMillis = Int(Date().timeIntervalSince1970 * 1000.0)
        let base = applicationSupportDirectory()
        let directory = base.appendingPathComponent("app-logs", isDirectory: true)
            .appendingPathComponent(session, isDirectory: true)

        appLogStarted = true
        appLogSessionID = session
        appLogStartedAtMs = startedMillis
        appLogStartUptime = ProcessInfo.processInfo.systemUptime
        appLogDirectory = directory
        appLogEvents.removeAll(keepingCapacity: true)
        appLogSequence = 0
        appLogBytes = 0
        appLogTruncated = false
        appLogLostEvents = false
        appLogSnapshotScheduled = false
        appLogSnapshotWriting = false
        appLogSnapshotDirty = false
        appLogRetry = RLAppLogRetryPolicy()

        registerAppLogObservers()
        let initialData = appLogData(events: [], truncated: false, lostEvents: false)
        let markerData = appLogMarkerData()
        if let initialData, let markerData {
            appLogIOQueue.async { [weak self] in
                guard let self else { return }
                var success = false
                do {
                    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                    try self.writeDurably(initialData, to: directory.appendingPathComponent("app-log.json"))
                    try self.writeDurably(markerData, to: base.appendingPathComponent("app-log-session.json"))
                    success = true
                } catch {
                    success = false
                }
                if !success {
                    DispatchQueue.main.async {
                        self.markAppLogLost()
                    }
                }
            }
        } else {
            markAppLogLost()
        }

        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.pollAppScreenOnMain()
        }
        appLogScreenTimer = timer
        RunLoop.main.add(timer, forMode: .common)
        pollAppScreenOnMain()
    }

    private func registerAppLogObservers() {
        guard appLogObservers.isEmpty else { return }
        let center = NotificationCenter.default
        let applicationEvents: [(Notification.Name, String)] = [
            (UIApplication.willEnterForegroundNotification, "foreground"),
            (UIApplication.didBecomeActiveNotification, "active"),
            (UIApplication.willResignActiveNotification, "inactive"),
            (UIApplication.didEnterBackgroundNotification, "background"),
            (UIApplication.willTerminateNotification, "termination_requested")
        ]
        appLogObservers = applicationEvents.map { notification, name in
            center.addObserver(forName: notification, object: nil, queue: .main) { [weak self] _ in
                if name == "background" { self?.hideObservedAppScreen() }
                self?.appendAppLogEvent(type: "lifecycle", name: name,
                                        component: "application", componentID: "app", target: nil)
            }
        }

        let sceneEvents: [(Notification.Name, String)] = [
            (UIScene.willConnectNotification, "connected"),
            (UIScene.didActivateNotification, "active"),
            (UIScene.willDeactivateNotification, "inactive"),
            (UIScene.willEnterForegroundNotification, "foreground"),
            (UIScene.didEnterBackgroundNotification, "background"),
            (UIScene.didDisconnectNotification, "disconnected")
        ]
        appLogObservers += sceneEvents.map { notification, name in
            center.addObserver(forName: notification, object: nil, queue: .main) { [weak self] note in
                guard let scene = note.object as? UIScene else {
                    self?.markAppLogLost()
                    return
                }
                if name == "background" || name == "disconnected" { self?.hideObservedAppScreen() }
                self?.appendAppLogEvent(type: "lifecycle", name: name,
                                        component: "scene", componentID: self?.componentID(for: scene) ?? "c0000000000000000",
                                        target: nil)
            }
        }
    }

    private func appendAppLogViewControllerEvent(_ name: String, controller: AnyObject) {
        guard Thread.isMainThread, let viewController = controller as? UIViewController,
              Bundle(for: type(of: viewController)).bundleURL == Bundle.main.bundleURL else { return }
        appendAppLogEvent(type: "screen", name: name, component: "view_controller",
                          componentID: componentID(for: controller),
                          target: componentID(for: controller))
    }

    private func appendAppLogClickBegin(action: Selector, target: Any?, sender: Any?) -> AppLogActionContext? {
        guard Thread.isMainThread, appLogStarted,
              let profile,
              let control = sender as? UIControl,
              control.allControlEvents == .touchUpInside,
              let identifier = control.accessibilityIdentifier else { return nil }
        let isBack = identifier == profile.backTarget
        let isTap = profile.tapTargets.contains(identifier)
        guard isBack || isTap else { return nil }
        guard profile.observationOnly || identifier != "counter.reset" || caseID == "reset" else { return nil }
        guard unambiguousTouchAction(control: control, target: target, action: action) else {
            markAppLogLost()
            return nil
        }
        let sequenceBefore = appLogSequence + 1
        let componentID = componentID(for: control)
        appendAppLogEvent(type: "click", name: "began", component: "view",
                          componentID: componentID, target: identifier)
        guard appLogSequence == sequenceBefore else { return nil }
        return AppLogActionContext(sequence: sequenceBefore, target: identifier, componentID: componentID)
    }

    private func appendAppLogClickResult(_ token: ActionToken, outcome: String) {
        guard let sequence = token.appLogSequence,
              let target = token.appLogTarget,
              let componentID = token.appLogComponentID else { return }
        _ = sequence
        appendAppLogEvent(type: "click", name: outcome, component: "view",
                          componentID: componentID, target: target)
    }

    private func markAppLogLost() {
        guard appLogStarted else { return }
        guard !appLogLostEvents else { return }
        appLogLostEvents = true
        appLogRetry.changed()
        enqueueAppLogSnapshot()
    }

    private func markAppLogTruncated() {
        guard appLogStarted, !appLogTruncated else { return }
        appLogTruncated = true
        appLogRetry.changed()
        enqueueAppLogSnapshot()
    }

    private func appendAppLogEvent(type: String,
                                   name: String,
                                   component: String,
                                   componentID: String,
                                   target: String?) {
        guard Thread.isMainThread, appLogStarted, !appLogTruncated else { return }
        let elapsed = appLogElapsedMs()
        guard elapsed <= Self.maxAppLogDurationMs else {
            markAppLogTruncated()
            return
        }
        guard appLogEvents.count < Self.maxAppLogEvents else {
            markAppLogTruncated()
            return
        }
        let event = AppLogEvent(seq: appLogSequence + 1, elapsedMs: elapsed,
                                type: type, name: name, component: component,
                                componentID: componentID, target: target)
        guard appLogBytes + Self.appLogEventReserveBytes <= Self.maxAppLogBytes else {
            markAppLogTruncated()
            return
        }
        appLogEvents.append(event)
        appLogSequence += 1
        appLogRetry.changed()
        appLogBytes += Self.appLogEventReserveBytes
        enqueueAppLogSnapshot()
    }

    private func enqueueAppLogSnapshot() {
        guard appLogStarted, appLogDirectory != nil else { return }
        guard appLogRetry.canSchedule() else { return }
        appLogSnapshotDirty = true
        guard !appLogSnapshotScheduled, !appLogSnapshotWriting else { return }
        appLogSnapshotScheduled = true
        let delay = appLogRetry.failures == 0 ? 0.05 : 0.2
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            self?.startAppLogSnapshotWrite()
        }
    }

    private func startAppLogSnapshotWrite() {
        guard Thread.isMainThread else { return }
        appLogSnapshotScheduled = false
        guard appLogStarted, !appLogSnapshotWriting, appLogSnapshotDirty,
              let directory = appLogDirectory else { return }
        appLogSnapshotDirty = false
        appLogSnapshotWriting = true
        appLogRetry.beginAttempt()
        let events = appLogEvents
        let truncated = appLogTruncated
        let lostEvents = appLogLostEvents
        let runID = self.runID
        let sessionID = appLogSessionID
        let profileDigest = self.profileDigest
        let startedAtMs = appLogStartedAtMs
        appLogIOQueue.async { [weak self] in
            guard let self else { return }
            var success = false
            do {
                guard let data = self.appLogData(events: events, truncated: truncated,
                                                 lostEvents: lostEvents, runID: runID,
                                                 sessionID: sessionID, profileDigest: profileDigest,
                                                 startedAtMs: startedAtMs),
                      data.count <= Self.maxAppLogBytes else {
                    throw NSError(domain: "RLAutomaticRecorder.appLog", code: 1)
                }
                try self.writeDurably(data, to: directory.appendingPathComponent("app-log.json"))
                success = true
            } catch {
                success = false
            }
            DispatchQueue.main.async {
                self.appLogSnapshotWriting = false
                self.appLogRetry.finished(success: success)
                if !success {
                    self.markAppLogLost()
                    self.appLogSnapshotDirty = true
                }
                if self.appLogSnapshotDirty {
                    self.enqueueAppLogSnapshot()
                }
            }
        }
    }

    private func appLogMarkerData() -> Data? {
        let object: [String: Any] = [
            "schemaVersion": 1,
            "platform": "ios",
            "applicationId": applicationID,
            "runId": runID,
            "sessionId": appLogSessionID,
            "profileDigest": profileDigest,
            "startedAtMs": appLogStartedAtMs
        ]
        return try? jsonData(object)
    }

    private func appLogData(events: [AppLogEvent], truncated: Bool, lostEvents: Bool,
                            runID: String = "", sessionID: String = "",
                            profileDigest: String = "", startedAtMs: Int = 0) -> Data? {
        let selectedRunID = runID.isEmpty ? self.runID : runID
        let selectedSessionID = sessionID.isEmpty ? appLogSessionID : sessionID
        let selectedDigest = profileDigest.isEmpty ? self.profileDigest : profileDigest
        let selectedStartedAtMs = startedAtMs == 0 ? appLogStartedAtMs : startedAtMs
        let object: [String: Any] = [
            "schemaVersion": 1,
            "platform": "ios",
            "applicationId": applicationID,
            "runId": selectedRunID,
            "sessionId": selectedSessionID,
            "profileDigest": selectedDigest,
            "startedAtMs": selectedStartedAtMs,
            "endSequence": events.count,
            "truncated": truncated,
            "lostEvents": lostEvents,
            "events": events.map { $0.object() }
        ]
        return try? jsonData(object)
    }

    private func appLogElapsedMs() -> Int {
        guard appLogStartUptime > 0 else { return 0 }
        return max(0, Int((ProcessInfo.processInfo.systemUptime - appLogStartUptime) * 1000.0))
    }

    private func recordAppLogSnapshotOnFinish() {
        guard appLogStarted else { return }
        enqueueAppLogSnapshot()
    }

    private func pollAppScreenOnMain() {
        guard Thread.isMainThread, appLogStarted,
              UIApplication.shared.applicationState == .active,
              let profile,
              let window = activeKeyWindow() else { return }
        let result = visibleConfiguredScreen(window: window, profile: profile)
        guard result.available else {
            markAppLogLost()
            return
        }
        guard let current = result.screen else {
            if let previous = observedScreen {
                appendAppLogEvent(type: "screen", name: "disappeared", component: "view",
                                  componentID: previous.componentID, target: previous.name)
                observedScreen = nil
            }
            return
        }
        guard let previous = observedScreen else {
            appendAppLogEvent(type: "screen", name: "appeared", component: "view",
                              componentID: current.componentID, target: current.name)
            observedScreen = current
            return
        }
        guard previous.name != current.name || previous.componentID != current.componentID else { return }
        appendAppLogEvent(type: "screen", name: "disappeared", component: "view",
                          componentID: previous.componentID, target: previous.name)
        appendAppLogEvent(type: "screen", name: "appeared", component: "view",
                          componentID: current.componentID, target: current.name)
        observedScreen = current
    }

    private func hideObservedAppScreen() {
        guard let previous = observedScreen else { return }
        appendAppLogEvent(type: "screen", name: "disappeared", component: "view",
                          componentID: previous.componentID, target: previous.name)
        observedScreen = nil
    }

    private func visibleConfiguredScreen(window: UIWindow, profile: Profile)
        -> (available: Bool, screen: ObservedScreen?) {
        var views: [(String, UIView)] = []
        let traversed = traverse(window, maxNodes: Self.maxViewNodes, maxDepth: Self.maxViewDepth) { view in
            guard let identifier = view.accessibilityIdentifier,
                  let name = profile.screenTargets[identifier],
                  isEffectivelyVisible(view, in: window) else { return true }
            views.append((name, view))
            return true
        }
        guard traversed, views.count <= 1 else { return (false, nil) }
        guard let (name, view) = views.first else { return (true, nil) }
        return (true, ObservedScreen(name: name, componentID: componentID(for: view)))
    }

    private func isEffectivelyVisible(_ view: UIView, in window: UIWindow) -> Bool {
        guard view.window === window else { return false }
        var current: UIView? = view
        while let node = current {
            if node.isHidden || node.alpha <= 0.01 { return false }
            current = node.superview
        }
        return true
    }

    private func componentID(for object: AnyObject) -> String {
        let className = String(describing: type(of: object))
        let digest = SHA256.hash(data: Data(className.utf8))
        let prefix = digest.prefix(8).map { String(format: "%02x", $0) }.joined()
        return "c\(prefix)"
    }

    private func registerLifecycleObservers() {
        guard lifecycleObservers.isEmpty else { return }
        let center = NotificationCenter.default
        let names: [Notification.Name] = [
            UIApplication.willResignActiveNotification,
            UIApplication.didEnterBackgroundNotification,
            UIScene.willDeactivateNotification,
            UIScene.didDisconnectNotification
        ]
        lifecycleObservers = names.map { name in
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                self?.lifecycleInterruptedOnMain()
            }
        }
        let wakeNames: [Notification.Name] = [
            UIApplication.didBecomeActiveNotification,
            UIWindow.didBecomeKeyNotification,
            UIScene.didActivateNotification
        ]
        lifecycleObservers += wakeNames.map { name in
            center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                guard let self, !self.started else { return }
                self.scheduleStartupAttempt()
            }
        }
    }

    private func lifecycleInterruptedOnMain() {
        guard Thread.isMainThread else { return }
        guard (started || starting), !finished else { return }
        lifecycleInterrupted = true
        captureInvalid = true
    }

    // MARK: - Darwin request and finalization

    private func installDarwinRequestObserver() {
        guard !requestObserverInstalled else { return }
        requestObserverInstalled = true
        let name = "io.reproloop.auto.freeze.\(runID)" as CFString
        let observer = Unmanaged.passUnretained(self).toOpaque()
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), observer,
                                         { _, rawObserver, _, _, _ in
                                             guard let rawObserver else { return }
                                             let recorder = Unmanaged<RLAutomaticRecorder>.fromOpaque(rawObserver).takeUnretainedValue()
                                             DispatchQueue.main.async {
                                                 recorder.finishOnMain()
                                             }
                                         }, name, nil, .deliverImmediately)
    }

    private func finishOnMain() {
        guard Thread.isMainThread else { return }
        if profile?.observationOnly == true {
            recordAppLogSnapshotOnFinish()
            return
        }
        guard started, captureEnabled, !finishing, !finished else {
            if started && !captureEnabled { captureInvalid = true }
            return
        }
        finishing = true

        guard pendingAction == nil,
              let profile,
              let window = activeKeyWindow(),
              flushTextOnMain(window: window, profile: profile),
              let finalSnapshot = snapshot(window: window, profile: profile, checkControlShapes: true) else {
            captureInvalid = true
            let snapshot = makeFinishSnapshot()
            enqueueExport(snapshot, valid: false)
            return
        }

        if elapsedMs() > Self.maxDurationMs {
            captureInvalid = true
        }
        let snapshot = makeFinishSnapshot(finalSnapshot: finalSnapshot)
        enqueueExport(snapshot, valid: !captureInvalid && !lifecycleInterrupted)
    }

    private struct FinishSnapshot {
        let events: [Event]
        let diagnostics: [[String: Any]]
        let endSequence: Int
        let startedAtMs: Int
        let sessionID: String
        let fixture: [String: Any]
        let startState: [String: Any]
        let valid: Bool
    }

    private func makeFinishSnapshot(finalSnapshot: Snapshot? = nil) -> FinishSnapshot {
        let fixture: [String: Any] = ["id": "ios-\(caseID)", "version": 1, "inputs": [:] as [String: String]]
        let startState: [String: Any] = ["screen": "main", "nodes": ["counter.name": "", "counter.count": "0"]]
        var valid = !captureInvalid && !lifecycleInterrupted && pendingAction == nil
        if let finalSnapshot {
            valid = valid && finalSnapshot.numeric.values.allSatisfy(Self.isValidNumeric)
        } else {
            valid = false
        }
        let actionEventIDs = events
            .filter { $0.action == "tap" || $0.action == "navigate_back" }
            .map(\.id)
        let diagnosticEventIDs = diagnostics.compactMap { $0["eventId"] as? String }
        valid = valid && actionEventIDs == diagnosticEventIDs
        return FinishSnapshot(events: events,
                              diagnostics: diagnostics,
                              endSequence: sequence,
                              startedAtMs: startedAtMs,
                              sessionID: sessionID,
                              fixture: fixture,
                              startState: startState,
                              valid: valid)
    }

    private func enqueueExport(_ snapshot: FinishSnapshot, valid: Bool) {
        let finalValid = valid && snapshot.valid
        let sessionURL = sessionDirectory
        let baseURL = baseDirectory
        let runID = self.runID
        let profileDigest = self.profileDigest
        let buildID = self.buildID
        let applicationID = Self.supportedApplicationID
        let events = snapshot.events
        let diagnostics = snapshot.diagnostics
        let fixture = snapshot.fixture
        let startState = snapshot.startState
        let startedMillis = snapshot.startedAtMs
        let session = snapshot.sessionID
        let endSequence = snapshot.endSequence

        ioQueue.async { [weak self] in
            guard let self else { return }
            let success = self.writeExport(valid: finalValid,
                                           sessionURL: sessionURL,
                                           baseURL: baseURL,
                                           runID: runID,
                                           profileDigest: profileDigest,
                                           buildID: buildID,
                                           applicationID: applicationID,
                                           events: events,
                                           diagnostics: diagnostics,
                                           fixture: fixture,
                                           startState: startState,
                                           startedAtMs: startedMillis,
                                           sessionID: session,
                                           endSequence: endSequence)
            DispatchQueue.main.async {
                self.finishState(success: success)
            }
        }
    }

    private func writeExport(valid: Bool,
                             sessionURL: URL?,
                             baseURL: URL?,
                             runID: String,
                             profileDigest: String,
                             buildID: String,
                             applicationID: String,
                             events: [Event],
                             diagnostics: [[String: Any]],
                             fixture: [String: Any],
                             startState: [String: Any],
                             startedAtMs: Int,
                             sessionID: String,
                             endSequence: Int) -> Bool {
        guard let sessionURL, let baseURL, events.count <= Self.maxActions else {
            postDarwin(name: "io.reproloop.auto.invalid.\(runID)")
            return false
        }

        do {
            let eventLines = try events.map { try jsonData($0.object()) + Data([10]) }
            let eventData = eventLines.reduce(into: Data()) { $0.append($1) }
            let capture: [String: Any] = [
                "schemaVersion": 2,
                "platform": "ios",
                "applicationId": applicationID,
                "sessionId": sessionID,
                "fixture": fixture,
                "startState": startState,
                "events": events.map { $0.object() },
                "truncated": !valid,
                "lostEvents": !valid,
                "endSequence": endSequence,
                "startedAtMs": startedAtMs
            ]
            let captureData = try jsonData(capture)
            let diagnosticsObject: [String: Any] = [
                "schemaVersion": 1,
                "platform": "ios",
                "runId": runID,
                "sessionId": sessionID,
                "profileDigest": profileDigest,
                "buildId": buildID,
                "endSequence": endSequence,
                "actions": diagnostics
            ]
            let diagnosticsData = try jsonData(diagnosticsObject)
            let totalBytes = eventData.count + captureData.count * 2 + diagnosticsData.count * 2
            guard totalBytes <= Self.maxCaptureBytes,
                  diagnosticsData.count <= Self.maxDiagnosticsBytes else {
                postDarwin(name: "io.reproloop.auto.invalid.\(runID)")
                return false
            }

            try writeDurably(eventData, to: sessionURL.appendingPathComponent("events.jsonl"))
            try writeDurably(captureData, to: baseURL.appendingPathComponent("capture.json"))
            try writeDurably(captureData, to: sessionURL.appendingPathComponent("capture.json"))
            try writeDurably(diagnosticsData, to: baseURL.appendingPathComponent("diagnostics.json"))
            try writeDurably(diagnosticsData, to: sessionURL.appendingPathComponent("diagnostics.json"))

            guard valid else {
                let marker: [String: Any] = ["finalized": false, "endSequence": endSequence]
                try writeDurably(jsonData(marker), to: sessionURL.appendingPathComponent("finalized.json"))
                postDarwin(name: "io.reproloop.auto.invalid.\(runID)")
                return false
            }

            let metadata = metadataObject(sessionID: sessionID, fixture: fixture,
                                          startedAtMs: startedAtMs, finalized: true,
                                          endSequence: endSequence)
            try writeDurably(jsonData(metadata), to: sessionURL.appendingPathComponent("metadata.json"))
            let finalMarker: [String: Any] = ["finalized": true, "endSequence": endSequence]
            try writeDurably(jsonData(finalMarker), to: sessionURL.appendingPathComponent("finalized.json"))
            let sessionMarker: [String: Any] = [
                "schemaVersion": 1,
                "runId": runID,
                "sessionId": sessionID,
                "profileDigest": profileDigest,
                "buildId": buildID,
                "fixture": fixture,
                "startedAtMs": startedAtMs,
                "finalized": true,
                "endSequence": endSequence
            ]
            try writeDurably(jsonData(sessionMarker), to: baseURL.appendingPathComponent("auto-session.json"))
            postDarwin(name: "io.reproloop.auto.finalized.\(runID)")
            return true
        } catch {
            postDarwin(name: "io.reproloop.auto.invalid.\(runID)")
            return false
        }
    }

    private func finishState(success: Bool) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in
                self?.finishState(success: success)
            }
            return
        }
        recordAppLogSnapshotOnFinish()
        started = false
        finishing = false
        finished = true
        captureEnabled = false
        startupMarkerData = nil
        removeDarwinRequestObserver()
        for observer in lifecycleObservers {
            NotificationCenter.default.removeObserver(observer)
        }
        lifecycleObservers.removeAll()
        _ = success
    }

    private func removeDarwinRequestObserver() {
        guard requestObserverInstalled else { return }
        let observer = Unmanaged.passUnretained(self).toOpaque()
        CFNotificationCenterRemoveObserver(CFNotificationCenterGetDarwinNotifyCenter(), observer,
                                           CFNotificationName("io.reproloop.auto.freeze.\(runID)" as CFString), nil)
        requestObserverInstalled = false
    }

    // MARK: - Action capture

    private func willSendActionOnMain(_ action: Selector,
                                      target: Any?,
                                      sender: Any?,
                                      event: UIEvent?) -> ActionToken? {
        guard Thread.isMainThread else {
            invalidateCapture()
            return nil
        }
        let appLogContext = appendAppLogClickBegin(action: action, target: target, sender: sender)
        func appLogOnlyToken() -> ActionToken? {
            guard let appLogContext else { return nil }
            return ActionToken(replayEventID: nil, replayTarget: nil, replayBefore: nil,
                               appLogSequence: appLogContext.sequence,
                               appLogTarget: appLogContext.target,
                               appLogComponentID: appLogContext.componentID)
        }

        if profile?.observationOnly == true { return appLogOnlyToken() }

        guard started, !finishing, !finished, !captureInvalid,
              let profile,
              let window = activeKeyWindow() else { return appLogOnlyToken() }
        guard captureEnabled else {
            captureInvalid = true
            return appLogOnlyToken()
        }
        guard pendingAction == nil else {
            invalidateCapture()
            return appLogOnlyToken()
        }

        // A toolbar UIBarButtonItem has no UIControl event mask, but it is a
        // bounded, accessibility-identified input commit surface. It may
        // flush an allowed text value without becoming a tap event.
        if let item = sender as? UIBarButtonItem {
            guard Self.textCommitIdentifiers.contains(item.accessibilityIdentifier ?? "") else { return nil }
            _ = flushTextOnMain(window: window, profile: profile)
            return appLogOnlyToken()
        }

        guard let control = sender as? UIControl else { return appLogOnlyToken() }

        // UITextField editingChanged and other control events deliberately do
        // not enter the action path. The event object is not trusted as a
        // logging source; control shape is the bounded discriminator.
        guard control.allControlEvents == .touchUpInside else { return appLogOnlyToken() }
        guard let identifier = control.accessibilityIdentifier else { return appLogOnlyToken() }
        let isBack = identifier == profile.backTarget
        let isTap = profile.tapTargets.contains(identifier)
        let isTextCommit = Self.textCommitIdentifiers.contains(identifier)
        guard isBack || isTap || isTextCommit else { return appLogOnlyToken() }
        guard unambiguousTouchAction(control: control, target: target, action: action) else {
            invalidateCapture()
            return appLogOnlyToken()
        }

        guard elapsedMs() <= Self.maxDurationMs else {
            invalidateCapture()
            return appLogOnlyToken()
        }
        guard flushTextOnMain(window: window, profile: profile) else { return appLogOnlyToken() }
        guard !captureInvalid else { return appLogOnlyToken() }

        guard !isTextCommit else { return appLogOnlyToken() }
        if identifier == "counter.reset" && caseID != "reset" {
            invalidateCapture()
            return appLogOnlyToken()
        }
        guard let snapshot = snapshot(window: window, profile: profile, checkControlShapes: false) else {
            invalidateCapture()
            return appLogOnlyToken()
        }
        let targetID = isBack ? profile.backTarget : identifier
        let actionName = isBack ? "navigate_back" : "tap"
        let nextSequence = sequence + 1
        guard nextSequence <= Self.maxActions else {
            invalidateCapture()
            return appLogOnlyToken()
        }
        let event = Event(id: "e\(nextSequence)", seq: nextSequence,
                          action: actionName, target: targetID, parameters: [:],
                          elapsedMs: elapsedMs())
        guard let lineData = try? jsonData(event.object()),
              eventBytes + lineData.count + 1 <= Self.maxCaptureBytes else {
            invalidateCapture()
            return appLogOnlyToken()
        }
        sequence = nextSequence
        eventBytes += lineData.count + 1
        events.append(event)
        let token = ActionToken(replayEventID: event.id, replayTarget: targetID,
                                replayBefore: snapshot,
                                appLogSequence: appLogContext?.sequence,
                                appLogTarget: appLogContext?.target,
                                appLogComponentID: appLogContext?.componentID)
        pendingAction = token
        return token
    }

    private func actionReturnedOnMain(_ token: ActionToken) {
        guard Thread.isMainThread else {
            invalidateCapture()
            return
        }
        appendAppLogClickResult(token, outcome: "returned")
        guard token.replayEventID != nil else { return }
        guard started, pendingAction === token else {
            invalidateCapture()
            return
        }
        appendDiagnostic(token: token, outcome: "returned")
        pendingAction = nil
    }

    private func actionThrewOnMain(_ token: ActionToken) {
        guard Thread.isMainThread else {
            invalidateCapture()
            return
        }
        appendAppLogClickResult(token, outcome: "threw")
        guard token.replayEventID != nil else { return }
        guard started, pendingAction === token else {
            invalidateCapture()
            return
        }
        appendDiagnostic(token: token, outcome: "threw")
        pendingAction = nil
    }

    private func appendDiagnostic(token: ActionToken, outcome: String) {
        guard let replayEventID = token.replayEventID,
              let replayTarget = token.replayTarget,
              let replayBefore = token.replayBefore,
              let window = activeKeyWindow(),
              let profile,
              let after = snapshot(window: window, profile: profile, checkControlShapes: false),
              outcome == "returned" || outcome == "threw" else {
            invalidateCapture()
            return
        }
        let action: [String: Any] = [
            "eventId": replayEventID,
            "target": replayTarget,
            "before": replayBefore.numeric,
            "after": after.numeric,
            "beforeScreen": replayBefore.screen,
            "afterScreen": after.screen,
            "outcome": outcome
        ]
        guard let data = try? jsonData(action),
              diagnostics.count < Self.maxActions,
              data.count <= Self.maxDiagnosticsBytes else {
            invalidateCapture()
            return
        }
        diagnostics.append(action)
    }

    private func unambiguousTouchAction(control: UIControl, target: Any?, action: Selector) -> Bool {
        guard let targetObject = target as AnyObject?,
              let actions = control.actions(forTarget: targetObject, forControlEvent: .touchUpInside),
              actions.count == 1,
              actions[0] == NSStringFromSelector(action),
              control.allTargets.count == 1,
              control.allControlEvents == .touchUpInside else { return false }
        return true
    }

    private func flushTextOnMain(window: UIWindow, profile: Profile) -> Bool {
        guard let snapshot = snapshot(window: window, profile: profile, checkControlShapes: false) else {
            invalidateCapture()
            return false
        }
        for target in profile.textTargets {
            let value = snapshot.text[target] ?? ""
            guard Self.isAllowedText(value) else {
                invalidateCapture()
                return false
            }
            guard elapsedMs() <= Self.maxDurationMs else {
                invalidateCapture()
                return false
            }
            guard lastCommittedText[target] != value else { continue }
            let nextSequence = sequence + 1
            guard nextSequence <= Self.maxActions else {
                invalidateCapture()
                return false
            }
            let event = Event(id: "e\(nextSequence)", seq: nextSequence,
                              action: "replace", target: target,
                              parameters: ["value": value], elapsedMs: elapsedMs())
            guard let lineData = try? jsonData(event.object()),
                  eventBytes + lineData.count + 1 <= Self.maxCaptureBytes else {
                invalidateCapture()
                return false
            }
            sequence = nextSequence
            eventBytes += lineData.count + 1
            events.append(event)
            lastCommittedText[target] = value
        }
        return true
    }

    private func invalidateCapture() {
        if Thread.isMainThread {
            captureInvalid = true
        } else {
            DispatchQueue.main.async { [weak self] in
                self?.captureInvalid = true
            }
        }
    }

    // MARK: - Bounded UIKit traversal and snapshots

    private func activeKeyWindow() -> UIWindow? {
        let windows = UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .filter { $0.activationState == .foregroundActive }
            .flatMap { $0.windows }
            .filter { $0.isKeyWindow && !$0.isHidden }
        guard windows.count == 1 else { return nil }
        return windows[0]
    }

    private func snapshot(window: UIWindow, profile: Profile, checkControlShapes: Bool) -> Snapshot? {
        var byIdentifier: [String: [UIView]] = [:]
        let traversed = traverse(window, maxNodes: Self.maxViewNodes, maxDepth: Self.maxViewDepth) { view in
            guard let identifier = view.accessibilityIdentifier else { return true }
            let relevant = profile.textTargets.contains(identifier)
                || profile.numericTargets.contains(identifier)
                || profile.screenTargets.keys.contains(identifier)
                || profile.tapTargets.contains(identifier)
                || identifier == profile.backTarget
            if relevant { byIdentifier[identifier, default: []].append(view) }
            return true
        }
        guard traversed else {
            return nil
        }

        let screenViews = byIdentifier.filter { profile.screenTargets.keys.contains($0.key) }
        guard screenViews.values.allSatisfy({ $0.count == 1 }), screenViews.count == 1,
              let screenID = screenViews.keys.first,
              let screen = profile.screenTargets[screenID] else { return nil }

        for target in profile.tapTargets.union([profile.backTarget]) {
            if let views = byIdentifier[target], views.count > 1 { return nil }
        }

        var text: [String: String] = [:]
        for target in profile.textTargets {
            guard let views = byIdentifier[target], views.count == 1,
                  let textField = views[0] as? UITextField,
                  !textField.isSecureTextEntry,
                  let value = textField.text else { return nil }
            text[target] = value
        }

        var numeric: [String: String] = [:]
        for target in profile.numericTargets {
            guard let views = byIdentifier[target], views.count == 1,
                  let value = textValue(of: views[0]),
                  Self.isValidNumeric(value) else { return nil }
            numeric[target] = value
        }

        if checkControlShapes {
            let requiredTargets = profile.tapTargets.filter { $0 != "counter.reset" || caseID == "reset" }
            for target in requiredTargets.union([profile.backTarget]) {
                guard let views = byIdentifier[target], views.count == 1,
                      let control = views[0] as? UIControl,
                      control.allControlEvents == .touchUpInside else { return nil }
            }
        }
        return Snapshot(screen: screen, text: text, numeric: numeric)
    }

    private func traverse(_ root: UIView,
                          maxNodes: Int,
                          maxDepth: Int,
                          visit: (UIView) -> Bool) -> Bool {
        var stack: [(UIView, Int)] = [(root, 0)]
        var count = 0
        while let (view, depth) = stack.popLast() {
            count += 1
            guard count <= maxNodes, depth <= maxDepth, visit(view) else { return false }
            for child in view.subviews.reversed() {
                stack.append((child, depth + 1))
            }
        }
        return true
    }

    private func textValue(of view: UIView) -> String? {
        if let label = view as? UILabel { return label.text }
        if let field = view as? UITextField { return field.text }
        return nil
    }

    private static func isAllowedText(_ value: String) -> Bool {
        value.isEmpty || value == "QA" || value == "Test"
    }

    private static func isValidNumeric(_ value: String) -> Bool {
        guard value.count >= 1, value.count <= 9 else { return false }
        return value.unicodeScalars.allSatisfy { $0.value >= 48 && $0.value <= 57 }
    }

    private static let textCommitIdentifiers: Set<String> = [
        "counter.keyboard_done",
        "counter.keyboard_accessory_done"
    ]

    // MARK: - Persistence helpers

    private func applicationSupportDirectory() -> URL {
        let root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return root.appendingPathComponent("ReproLoop", isDirectory: true)
    }

    private func publishRuntimeIdentity(startedAtMs: Int) {
        let base = applicationSupportDirectory()
        let sanitation = Self.currentSanitationReceipt()
        guard armSanitationCleanup(startedAtMs: startedAtMs) else { return }
        ioQueue.async { [weak self] in
            guard let self else { return }
            do {
                try ReproRuntimeIdentityWriter.write(bundleID: self.applicationID,
                                                     buildID: self.buildID,
                                                     runID: self.runID,
                                                     profileDigest: self.profileDigest,
                                                     startedAtMs: startedAtMs,
                                                     sanitation: sanitation,
                                                     to: base)
            } catch {
                return
            }
        }
    }

    private static func currentSanitationReceipt() -> [String: Any]? {
        guard Thread.isMainThread,
              let runtime = NSClassFromString("RLSanitationRuntime") as? RLSanitationRuntimeBridge.Type,
              let receipt = runtime.currentLaunchReceipt() else { return nil }
        return receipt as? [String: Any]
    }

    private func armSanitationCleanup(startedAtMs: Int) -> Bool {
        guard Thread.isMainThread else { return false }
        guard let runtime = NSClassFromString("RLSanitationRuntime") as? RLSanitationRuntimeBridge.Type else {
            return ProcessInfo.processInfo.environment["REPRO_SANITATION_POLICY_DIGEST"] == nil
        }
        return runtime.completeValidatedStartup(bundleID: applicationID,
                                                buildID: buildID,
                                                profileDigest: profileDigest,
                                                startedAtMs: startedAtMs)
    }

    private func metadataObject(sessionID: String,
                                fixture: [String: Any],
                                startedAtMs: Int,
                                finalized: Bool,
                                endSequence: Int?) -> [String: Any] {
        var metadata: [String: Any] = [
            "schemaVersion": 2,
            "platform": "ios",
            "applicationId": Self.supportedApplicationID,
            "sessionId": sessionID,
            "fixture": fixture,
            "startState": ["screen": "main", "nodes": ["counter.name": "", "counter.count": "0"]],
            "startedAtMs": startedAtMs,
            "finalized": finalized
        ]
        if let endSequence { metadata["endSequence"] = endSequence }
        return metadata
    }

    private func jsonData(_ object: Any) throws -> Data {
        guard JSONSerialization.isValidJSONObject(object) else { throw NSError(domain: "RLAutomaticRecorder", code: 1) }
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    private func writeDurably(_ data: Data, to url: URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: url, options: [.atomic])
        let handle = try FileHandle(forReadingFrom: url)
        try handle.synchronize()
        try handle.close()
    }

    private func postDarwin(name: String) {
        CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(),
                                              CFNotificationName(name as CFString), nil, nil, true)
    }

    private func elapsedMs() -> Int {
        guard startUptime > 0 else { return 0 }
        let value = Int((ProcessInfo.processInfo.systemUptime - startUptime) * 1000.0)
        return max(0, value)
    }

    private func isDigest(_ value: String) -> Bool {
        value.count == 64 && value.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57) || ($0.value >= 65 && $0.value <= 70) || ($0.value >= 97 && $0.value <= 102)
        }
    }

    private func isBuildID(_ value: String) -> Bool {
        guard value.count >= 8, value.count <= 128 else { return false }
        return value.unicodeScalars.allSatisfy {
            ($0.value >= 48 && $0.value <= 57)
                || ($0.value >= 65 && $0.value <= 90)
                || ($0.value >= 97 && $0.value <= 122)
                || $0.value == 45
                || $0.value == 95
        }
    }

    private func isAppLogSchemaEnabled(_ value: Any?) -> Bool {
        guard let number = value as? NSNumber,
              CFGetTypeID(number) == CFNumberGetTypeID() else { return false }
        return number.intValue == 1
    }
}
#endif
