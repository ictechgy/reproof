import XCTest
import CoreFoundation

final class ReproReplayTests: XCTestCase {
    private struct Scenario: Decodable {
        struct Step: Decodable {
            let action: String
            let target: String
            let parameters: [String: String]?
            let sourceEventIds: [String]
        }

        struct Oracle: Decodable {
            struct Condition: Decodable {
                let target: String
                let text: String
            }
            let bugCondition: Condition
            let expectedCondition: Condition
        }

        struct Fixture: Decodable {
            let id: String
            let version: Int
            let inputs: [String: String]
        }

        let mode: String
        let runId: String
        let autoProfileDigest: String?
        let expectedBuildId: String
        let scenarioDigest: String
        let steps: [Step]
        let oracle: Oracle
        let fixture: Fixture
    }

    private struct StepResult: Encodable {
        let action: String
        let target: String
        let sourceEventIds: [String]
    }

    private struct ReplayResult: Encodable {
        let schemaVersion: Int
        let runId: String
        let scenarioDigest: String
        let buildId: String
        let runValid: Bool
        let bugCondition: Bool
        let expectedCondition: Bool
        let finalNodes: [String: String]
        let steps: [StepResult]
        let mode: String
        let autoRunId: String?
        let profileDigest: String?
    }

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    func testScenario() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let encoded = environment["REPRO_SCENARIO_B64"],
              let data = Data(base64Encoded: encoded),
              data.count <= 16 * 1024 else {
            XCTFail("missing or oversized REPRO_SCENARIO_B64")
            return
        }
        let scenario: Scenario
        do {
            scenario = try JSONDecoder().decode(Scenario.self, from: data)
        } catch {
            XCTFail("invalid scenario payload: \(error)")
            return
        }
        try validate(scenario)

        let app = XCUIApplication(bundleIdentifier: "io.reproof.sample.ios")
        app.launchEnvironment["REPRO_MODE"] = scenario.mode
        app.launchEnvironment["REPRO_FIXTURE_RESET"] = "1"
        app.launchEnvironment["REPRO_RUN_ID"] = scenario.runId
        app.launchEnvironment["REPRO_CASE"] = reproCase(for: scenario.fixture.id)
        if let profileDigest = scenario.autoProfileDigest {
            app.launchEnvironment["REPRO_AUTO_PROFILE_DIGEST"] = profileDigest
        }
        let autoReady = scenario.mode == "record" && scenario.autoProfileDigest != nil
            ? DarwinAutoReadyWaiter(runID: scenario.runId) : nil
        app.launch()
        if let autoReady, !autoReady.wait(timeout: 10) {
            throw ReplayError.invalid("automatic recorder did not become ready")
        }

        let name = app.textFields["counter.name"]
        let count = app.staticTexts["counter.count"]
        let buildID = app.staticTexts["counter.build_id"]
        XCTAssertTrue(name.waitForExistence(timeout: 5), "counter.name missing")
        XCTAssertTrue(count.waitForExistence(timeout: 5), "counter.count missing")
        XCTAssertTrue(buildID.waitForExistence(timeout: 5), "counter.build_id missing")
        XCTAssertEqual(name.value as? String ?? "", "")
        XCTAssertEqual(count.label, "0")
        XCTAssertEqual(buildID.label, "Build: \(scenario.expectedBuildId)", "unexpected build identity")

        var stepResults: [StepResult] = []
        for step in scenario.steps {
            try perform(step, app: app)
            stepResults.append(StepResult(action: step.action, target: step.target, sourceEventIds: step.sourceEventIds))
        }

        var autoRunId: String?
        var profileDigest: String?
        if scenario.mode == "record", let digest = scenario.autoProfileDigest {
            guard DarwinAutoCapture.freeze(runID: scenario.runId) else {
                throw ReplayError.invalid("automatic capture acknowledgement failed")
            }
            autoRunId = scenario.runId
            profileDigest = digest
        } else if scenario.mode == "record" {
            let report = app.buttons["counter.report"]
            XCTAssertTrue(report.waitForExistence(timeout: 3), "counter.report missing")
            XCTAssertTrue(report.isHittable, "counter.report not hittable")
            report.tap()
            let ready = app.staticTexts["counter.capture_ready"]
            XCTAssertTrue(ready.waitForExistence(timeout: 5), "capture was not durably finalized")
        }

        let finalName = name.value as? String ?? ""
        let finalCount = count.label
        let bugCondition = matches(scenario.oracle.bugCondition, target: "counter.count", text: finalCount)
        let expectedCondition = matches(scenario.oracle.expectedCondition, target: "counter.count", text: finalCount)
        let result = ReplayResult(
            schemaVersion: 1,
            runId: scenario.runId,
            scenarioDigest: scenario.scenarioDigest,
            buildId: String(buildID.label.dropFirst("Build: ".count)),
            runValid: true,
            bugCondition: bugCondition,
            expectedCondition: expectedCondition,
            finalNodes: ["counter.name": finalName, "counter.count": finalCount],
            steps: stepResults,
            mode: scenario.mode,
            autoRunId: autoRunId,
            profileDigest: profileDigest
        )
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let attachment = XCTAttachment(data: try encoder.encode(result), uniformTypeIdentifier: "public.json")
        attachment.name = "repro-result.json"
        attachment.lifetime = .keepAlways
        add(attachment)
        app.terminate()
    }

    private func validate(_ scenario: Scenario) throws {
        guard scenario.mode == "record" || scenario.mode == "replay" else {
            throw ReplayError.invalid("unsupported mode")
        }
        guard !scenario.runId.isEmpty, !scenario.expectedBuildId.isEmpty, !scenario.scenarioDigest.isEmpty else {
            throw ReplayError.invalid("missing run identity")
        }
        if let digest = scenario.autoProfileDigest {
            guard digest.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
                throw ReplayError.invalid("invalid automatic profile digest")
            }
        }
        guard ["ios-counter", "ios-duplicate-submit", "ios-reset"].contains(scenario.fixture.id),
              scenario.fixture.version == 1,
              scenario.fixture.inputs.isEmpty else {
            throw ReplayError.invalid("unsupported fixture")
        }
        let oracle: (bug: String, expected: String)
        switch scenario.fixture.id {
        case "ios-counter", "ios-duplicate-submit":
            oracle = ("2", "1")
        case "ios-reset":
            oracle = ("1", "0")
        default:
            throw ReplayError.invalid("unsupported fixture")
        }
        guard scenario.oracle.bugCondition.target == "counter.count",
              scenario.oracle.bugCondition.text == oracle.bug,
              scenario.oracle.expectedCondition.target == "counter.count",
              scenario.oracle.expectedCondition.text == oracle.expected else {
            throw ReplayError.invalid("unsupported oracle")
        }
        var ids = Set<String>()
        for step in scenario.steps {
            guard !step.sourceEventIds.isEmpty else {
                throw ReplayError.invalid("missing or duplicate sourceEventIds")
            }
            for sourceEventID in step.sourceEventIds {
                guard ids.insert(sourceEventID).inserted else {
                    throw ReplayError.invalid("missing or duplicate sourceEventIds")
                }
            }
            guard ["tap", "replace", "scroll_to", "navigate_back"].contains(step.action) else {
                throw ReplayError.invalid("unsupported action")
            }
            switch step.action {
            case "tap":
                var allowedTargets = ["counter.add", "counter.next", "counter.back"]
                if scenario.fixture.id == "ios-reset" {
                    allowedTargets.append("counter.reset")
                }
                guard allowedTargets.contains(step.target) else {
                    throw ReplayError.invalid("unsupported tap target")
                }
            case "replace":
                guard step.target == "counter.name", ["", "QA", "Test"].contains(step.parameters?["value"] ?? "") else {
                    throw ReplayError.invalid("unsupported replacement")
                }
            case "scroll_to":
                guard step.target == "counter.bottom", step.parameters?["container"] == "counter.list", step.parameters?["direction"] == "forward" else {
                    throw ReplayError.invalid("unsupported scroll")
                }
            case "navigate_back":
                guard step.target == "counter.back" else { throw ReplayError.invalid("unsupported back") }
            default:
                throw ReplayError.invalid("unsupported action")
            }
        }
    }

    private func reproCase(for fixtureID: String) -> String {
        switch fixtureID {
        case "ios-counter":
            return "counter"
        case "ios-duplicate-submit":
            return "duplicate-submit"
        case "ios-reset":
            return "reset"
        default:
            return "counter"
        }
    }

    private func perform(_ step: Scenario.Step, app: XCUIApplication) throws {
        switch step.action {
        case "replace":
            let clear = app.buttons["counter.clear"]
            let field = app.textFields["counter.name"]
            XCTAssertTrue(clear.waitForExistence(timeout: 3), "counter.clear missing")
            XCTAssertTrue(field.waitForExistence(timeout: 3), "counter.name missing")
            clear.tap()
            field.tap()
            let value = step.parameters?["value"] ?? ""
            if !value.isEmpty { field.typeText(value) }
            let done = app.buttons["counter.keyboard_done"]
            XCTAssertTrue(done.waitForExistence(timeout: 3) && done.isHittable, "explicit commit control missing")
            done.tap()
            XCTAssertEqual(field.value as? String ?? "", step.parameters?["value"] ?? "")
        case "tap":
            let button = app.buttons[step.target]
            XCTAssertTrue(button.waitForExistence(timeout: 3), "\(step.target) missing")
            XCTAssertTrue(button.isHittable, "\(step.target) not hittable")
            button.tap()
        case "scroll_to":
            let container = app.scrollViews["counter.list"]
            let target = app.staticTexts["counter.bottom"]
            XCTAssertTrue(container.waitForExistence(timeout: 3), "counter.list missing")
            var found = false
            for _ in 0..<8 {
                if target.exists && target.isHittable {
                    found = true
                    break
                }
                container.swipeUp()
            }
            XCTAssertTrue(found || target.isHittable, "counter.bottom did not become hittable")
        case "navigate_back":
            let button = app.buttons["counter.back"]
            XCTAssertTrue(button.waitForExistence(timeout: 3), "counter.back missing")
            XCTAssertTrue(button.isHittable, "counter.back not hittable")
            button.tap()
        default:
            throw ReplayError.invalid("unsupported action")
        }
    }

    private func matches(_ condition: Scenario.Oracle.Condition, target: String, text: String) -> Bool {
        condition.target == target && condition.text == text
    }

    private enum ReplayError: Error {
        case invalid(String)
    }
}

private final class DarwinAutoCapture {
    private let center = CFNotificationCenterGetDarwinNotifyCenter()
    private let request: String
    private let finalized: String
    private let invalid: String
    private var observer: UnsafeRawPointer { UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque()) }
    private let lock = NSLock()
    private var outcome: String?

    private init(runID: String) {
        request = "io.reproof.auto.freeze.\(runID.lowercased())"
        finalized = "io.reproof.auto.finalized.\(runID.lowercased())"
        invalid = "io.reproof.auto.invalid.\(runID.lowercased())"
        CFNotificationCenterAddObserver(center, observer, { _, observer, name, _, _ in
            guard let observer, let name else { return }
            let waiter = Unmanaged<DarwinAutoCapture>.fromOpaque(UnsafeMutableRawPointer(mutating: observer)).takeUnretainedValue()
            waiter.receive(name.rawValue as String)
        }, finalized as CFString, nil, .deliverImmediately)
        CFNotificationCenterAddObserver(center, observer, { _, observer, name, _, _ in
            guard let observer, let name else { return }
            let waiter = Unmanaged<DarwinAutoCapture>.fromOpaque(UnsafeMutableRawPointer(mutating: observer)).takeUnretainedValue()
            waiter.receive(name.rawValue as String)
        }, invalid as CFString, nil, .deliverImmediately)
    }

    static func freeze(runID: String) -> Bool {
        guard UUID(uuidString: runID) != nil else { return false }
        let waiter = DarwinAutoCapture(runID: runID)
        return waiter.wait(timeout: 10)
    }

    deinit {
        CFNotificationCenterRemoveObserver(center, observer, CFNotificationName(rawValue: finalized as CFString), nil)
        CFNotificationCenterRemoveObserver(center, observer, CFNotificationName(rawValue: invalid as CFString), nil)
    }

    private func wait(timeout: TimeInterval) -> Bool {
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

private final class DarwinAutoReadyWaiter {
    private let center = CFNotificationCenterGetDarwinNotifyCenter()
    private let name: String
    private var observer: UnsafeRawPointer { UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque()) }
    private let lock = NSLock()
    private var received = false

    init(runID: String) {
        name = "io.reproof.auto.ready.\(runID.lowercased())"
        CFNotificationCenterAddObserver(center, observer, { _, observer, _, _, _ in
            guard let observer else { return }
            let waiter = Unmanaged<DarwinAutoReadyWaiter>.fromOpaque(UnsafeMutableRawPointer(mutating: observer)).takeUnretainedValue()
            waiter.markReceived()
        }, name as CFString, nil, .deliverImmediately)
    }

    deinit {
        CFNotificationCenterRemoveObserver(center, observer, CFNotificationName(rawValue: name as CFString), nil)
    }

    func wait(timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(min(timeout, 10))
        while Date() < deadline {
            lock.lock()
            let done = received
            lock.unlock()
            if done { return true }
            _ = RunLoop.current.run(mode: .default, before: min(deadline, Date().addingTimeInterval(0.05)))
        }
        return false
    }

    private func markReceived() {
        lock.lock()
        received = true
        lock.unlock()
    }
}
