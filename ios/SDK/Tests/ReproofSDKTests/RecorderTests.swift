import Foundation
import XCTest
@testable import ReproofSDK

final class RecorderTests: XCTestCase {
    private var root: URL!
    override func setUpWithError() throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    }
    override func tearDownWithError() throws {
        if FileManager.default.fileExists(atPath: root.path) { try FileManager.default.removeItem(at: root) }
    }
    private func policy() -> CapturePolicy {
        CapturePolicy(screens: ["main"], startNodeValues: ["counter.count": ["0"], "counter.name": [""]],
            tapTargets: ["counter.add"],
            replacementValues: ["counter.name": ["", "QA", "Test"]],
            scrollRules: [ScrollRule(target: "counter.bottom", container: "counter.list")],
            backTargets: ["counter.back"])
    }
    private func config(policy: CapturePolicy? = nil, fixture: FixtureMetadata? = nil,
                        start: StartState? = nil, limits: CaptureLimits = CaptureLimits(),
                        application: String = "io.reproof.sample.ios", directory: URL? = nil) -> RecorderConfiguration {
        RecorderConfiguration(rootDirectory: directory ?? root, application: ApplicationMetadata(id: application),
            fixture: fixture ?? FixtureMetadata(id: "ios-counter", version: 1),
            startState: start ?? StartState(screen: "main", nodes: ["counter.count": "0", "counter.name": ""]),
            policy: policy ?? self.policy(), limits: limits)
    }
    private func finish(_ recorder: ReproofRecorder, file: StaticString = #filePath, line: UInt = #line) -> FinishResult {
        let done = expectation(description: "finish")
        var result: FinishResult = .inactive
        recorder.finish { result = $0; done.fulfill() }
        wait(for: [done], timeout: 5)
        return result
    }
    private func exported(_ recorder: ReproofRecorder, file: StaticString = #filePath, line: UInt = #line) throws -> SessionExport {
        guard case let .finished(value) = finish(recorder) else {
            XCTFail("Expected finalized export", file: file, line: line)
            throw NSError(domain: "synthetic-test", code: 1)
        }
        return value
    }
    private func json(_ url: URL) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }
    private func failed(_ result: FinishResult, file: StaticString = #filePath, line: UInt = #line) {
        if case .failed = result {} else { XCTFail("Expected fixed failure", file: file, line: line) }
    }
    private func bytes() throws -> Data {
        guard let files = FileManager.default.enumerator(at: root, includingPropertiesForKeys: [.isRegularFileKey]) else { return Data() }
        var data = Data()
        for case let file as URL in files {
            if try file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true { data.append(try Data(contentsOf: file)) }
        }
        return data
    }
    private func sessionDirectory() throws -> URL {
        try XCTUnwrap(FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)
            .first { $0.lastPathComponent != "capture.json" })
    }
    private struct FinalizationFlag: Decodable { let finalized: Bool }
    private func finalizedFlag(_ url: URL) throws -> Bool {
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == CocoaError.Code.fileReadNoSuchFile.rawValue {
            // A missing file is expected negative evidence, not an XCTest failure.
            return false
        }
        // Present malformed/unreadable documents must not be treated as absent.
        return try JSONDecoder().decode(FinalizationFlag.self, from: data).finalized
    }
    private func hasFinalizedPair(_ session: URL) throws -> Bool {
        let metadataFinal = try finalizedFlag(session.appendingPathComponent("metadata.json"))
        let markerFinal = try finalizedFlag(session.appendingPathComponent("finalized.json"))
        return metadataFinal && markerFinal
    }
    private func assertNotFinalized() throws {
        XCTAssertFalse(try hasFinalizedPair(sessionDirectory()))
    }

    func testFinalizationHelperDistinguishesAbsenceFalseFlagsAndTruePair() throws {
        let session = root.appendingPathComponent("helper", isDirectory: true)
        try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
        let metadata = session.appendingPathComponent("metadata.json")
        let marker = session.appendingPathComponent("finalized.json")
        let falseFlag = Data("{\"finalized\":false}".utf8)
        let trueFlag = Data("{\"finalized\":true}".utf8)
        XCTAssertFalse(try hasFinalizedPair(session))
        try assertNotFinalized()
        try falseFlag.write(to: metadata)
        XCTAssertFalse(try hasFinalizedPair(session))
        try assertNotFinalized()
        try falseFlag.write(to: marker)
        XCTAssertFalse(try hasFinalizedPair(session))
        try trueFlag.write(to: metadata)
        XCTAssertFalse(try hasFinalizedPair(session))
        try falseFlag.write(to: metadata)
        try trueFlag.write(to: marker)
        XCTAssertFalse(try hasFinalizedPair(session))
        try trueFlag.write(to: metadata)
        XCTAssertTrue(try hasFinalizedPair(session))
        try FileManager.default.removeItem(at: metadata)
        XCTAssertFalse(try hasFinalizedPair(session))
    }

    func testFinalizationHelperRejectsPresentMalformedAndUnreadableDocuments() throws {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false)
        let metadata = root.appendingPathComponent("metadata.json")
        let marker = root.appendingPathComponent("finalized.json")
        let falseFlag = Data("{\"finalized\":false}".utf8)
        let malformed = ["{", "[]", "{}", "{\"finalized\":\"false\"}", "{\"finalized\":1}", "{\"finalized\":null}"]
        try falseFlag.write(to: marker)
        for text in malformed {
            try Data(text.utf8).write(to: metadata)
            XCTAssertThrowsError(try hasFinalizedPair(root))
        }
        try falseFlag.write(to: metadata)
        for text in malformed {
            try Data(text.utf8).write(to: marker)
            XCTAssertThrowsError(try hasFinalizedPair(root))
        }
        try FileManager.default.removeItem(at: metadata)
        try FileManager.default.createDirectory(at: metadata, withIntermediateDirectories: false)
        XCTAssertThrowsError(try hasFinalizedPair(root))
        try FileManager.default.removeItem(at: metadata)
        try falseFlag.write(to: metadata)
        try FileManager.default.removeItem(at: marker)
        try FileManager.default.createDirectory(at: marker, withIntermediateDirectories: false)
        XCTAssertThrowsError(try hasFinalizedPair(root))
    }

    func testSampleV2KeysPathsAndActionOrder() throws {
        let recorder = ReproofRecorder(configuration: config())
        XCTAssertEqual(recorder.startIfNeeded(), .started)
        XCTAssertEqual(recorder.startIfNeeded(), .alreadyStarted)
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .accepted)
        XCTAssertEqual(recorder.recordReplace(target: "counter.name", value: "QA"), .accepted)
        XCTAssertEqual(recorder.recordScroll(target: "counter.bottom", container: "counter.list"), .accepted)
        XCTAssertEqual(recorder.recordBack(target: "counter.back"), .accepted)
        let paths = try exported(recorder)
        XCTAssertEqual(paths.capture.deletingLastPathComponent(), paths.directory)
        let capture = try json(paths.capture), metadata = try json(paths.metadata), marker = try json(paths.marker)
        XCTAssertEqual(Set(capture.keys), ["schemaVersion", "platform", "applicationId", "sessionId", "fixture", "startState", "events", "truncated", "lostEvents", "endSequence", "startedAtMs"])
        XCTAssertEqual(capture["schemaVersion"] as? Int, 2)
        XCTAssertEqual(capture["platform"] as? String, "ios")
        XCTAssertEqual(capture["applicationId"] as? String, "io.reproof.sample.ios")
        XCTAssertEqual(capture["sessionId"] as? String, paths.directory.lastPathComponent)
        XCTAssertNotNil(UUID(uuidString: paths.directory.lastPathComponent))
        XCTAssertEqual(metadata["sessionId"] as? String, capture["sessionId"] as? String)
        XCTAssertEqual(metadata["startedAtMs"] as? Int, capture["startedAtMs"] as? Int)
        XCTAssertEqual(metadata["finalized"] as? Bool, true)
        XCTAssertEqual(metadata["endSequence"] as? Int, 4)
        XCTAssertEqual(marker["endSequence"] as? Int, 4)
        XCTAssertEqual(capture["truncated"] as? Bool, false)
        XCTAssertEqual(capture["lostEvents"] as? Bool, false)
        let events = try XCTUnwrap(capture["events"] as? [[String: Any]])
        XCTAssertEqual(events.compactMap { $0["action"] as? String }, ["tap", "replace", "scroll_to", "navigate_back"])
        XCTAssertEqual(events.compactMap { $0["seq"] as? Int }, [1, 2, 3, 4])
        XCTAssertEqual(events.compactMap { $0["id"] as? String }, ["e1", "e2", "e3", "e4"])
        let lines = try String(contentsOf: paths.events, encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(lines.count, 4)
        for (index, line) in lines.enumerated() {
            let event = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
            XCTAssertEqual(event?["seq"] as? Int, index + 1)
        }
        XCTAssertEqual(try Data(contentsOf: paths.capture), try Data(contentsOf: root.appendingPathComponent("capture.json")))
        XCTAssertEqual(recorder.startIfNeeded(), .finished)
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .inactive)
    }

    func testCounterExportForUnchangedHostBridge() throws {
        let recorder = ReproofRecorder(configuration: config())
        recorder.startIfNeeded()
        XCTAssertEqual(recorder.recordReplace(target: "counter.name", value: "QA"), .accepted)
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .accepted)
        let paths = try exported(recorder)
        XCTAssertEqual(try json(paths.capture)["endSequence"] as? Int, 2)
        // CI opts into retaining only these synthetic exact writer bytes locally.
        // No test export is uploaded; the separate host check prints a safe summary.
        if let output = ProcessInfo.processInfo.environment["REPRO_SDK_TEST_EXPORT_DIRECTORY"] {
            let destination = URL(fileURLWithPath: output, isDirectory: true)
            XCTAssertFalse(FileManager.default.fileExists(atPath: destination.path))
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: false)
            for source in [paths.capture, paths.metadata, paths.marker] {
                try FileManager.default.copyItem(at: source, to: destination.appendingPathComponent(source.lastPathComponent))
            }
        }
    }

    func testInvalidConfigurationHasNoClockUUIDQueueOrFileEffects() {
        var env = RecorderEnvironment.live
        env.wallTimeMs = { XCTFail("Invalid config read clock"); return 0 }
        env.uptime = { XCTFail("Invalid config read uptime"); return 0 }
        env.sessionID = { XCTFail("Invalid config created UUID"); return "" }
        env.createDirectory = { _ in XCTFail("Invalid config created directory") }
        let recorder = ReproofRecorder(configuration: config(start: StartState(screen: "main", nodes: ["counter.name": "PRIVATE_CANARY"])), environment: env)
        XCTAssertFalse(recorder.isEnabled)
        XCTAssertEqual(recorder.startIfNeeded(), .disabled)
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .inactive)
        if case .inactive = finish(recorder) {} else { XCTFail("Disabled finish") }
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
    }

    func testConfigurationBoundsAndDefaultDeny() {
        let invalid: [RecorderConfiguration] = [
            config(policy: CapturePolicy()),
            config(application: "bad/application"),
            config(application: String(repeating: "a", count: 129)),
            config(directory: URL(string: "https://example.invalid/export")!),
            config(fixture: FixtureMetadata(id: "fixture", version: 0)),
            config(start: StartState(screen: "PRIVATE_CANARY", nodes: [:])),
            config(start: StartState(screen: "main", nodes: ["unknown": "0"])),
            config(start: StartState(screen: "main", nodes: [:])),
            config(limits: CaptureLimits(maxEvents: 0)), config(limits: CaptureLimits(maxEvents: 257)),
            config(limits: CaptureLimits(maxDurationMs: 600_001)), config(limits: CaptureLimits(maxDurationMs: 0)),
            config(limits: CaptureLimits(maxBytes: Int.max)), config(limits: CaptureLimits(maxBytes: 524_287)),
            config(policy: CapturePolicy(screens: ["main"], tapTargets: Set((0..<33).map { "tap\($0)" }))),
            config(policy: CapturePolicy(screens: ["main"], replacementValues: ["input": [String(repeating: "a", count: 257)]])),
            config(policy: CapturePolicy(screens: ["main"], replacementValues: ["input": ["\n"]]))
        ]
        for configuration in invalid {
            XCTAssertEqual(ReproofRecorder(configuration: configuration).startIfNeeded(), .disabled)
        }
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
    }

    func testCustomPolicyRemainsLocalAnnotationsWithEmptyFixtureInputs() throws {
        let custom = CapturePolicy(screens: ["inventory"], startNodeValues: ["stock.count": ["1"]],
            tapTargets: ["stock.save"])
        let recorder = ReproofRecorder(configuration: config(policy: custom,
            fixture: FixtureMetadata(id: "inventory-fixture", version: 2),
            start: StartState(screen: "inventory", nodes: ["stock.count": "1"]), application: "example.inventory"))
        recorder.startIfNeeded()
        XCTAssertEqual(recorder.recordTap(target: "stock.save"), .accepted)
        let capture = try json(exported(recorder).capture)
        XCTAssertEqual(capture["applicationId"] as? String, "example.inventory")
        XCTAssertNil(capture["dispatch"]); XCTAssertNil(capture["logs"]); XCTAssertNil(capture["buildId"])
    }

    func testDeniedValuesNeverPersistOrReadClock() throws {
        let canary = "PRIVATE_CANARY_DO_NOT_RETAIN"
        for operation in 0..<5 {
            root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            var env = RecorderEnvironment.live
            let reads = LockedCounter()
            env.uptime = { reads.increment(); return 1 }
            let recorder = ReproofRecorder(configuration: config(), environment: env)
            recorder.startIfNeeded()
            let before = reads.value
            let result: RecordResult
            switch operation {
            case 0: result = recorder.recordReplace(target: "counter.name", value: canary)
            case 1: result = recorder.recordTap(target: canary)
            case 2: result = recorder.recordScroll(target: "counter.bottom", container: canary)
            case 3: result = recorder.recordBack(target: canary)
            default: result = recorder.recordReplace(target: "counter.name", value: String(repeating: canary, count: 10000))
            }
            XCTAssertEqual(result, .rejected)
            XCTAssertEqual(reads.value, before)
            failed(finish(recorder)); try assertNotFinalized()
            XCTAssertNil(try bytes().range(of: Data(canary.utf8)))
            try FileManager.default.removeItem(at: root)
        }
    }

    func testActionSpecificRulesAndExactMembership() throws {
        let operations: [(ReproofRecorder) -> RecordResult] = [
            { $0.recordTap(target: "counter.back") },
            { $0.recordBack(target: "counter.add") },
            { $0.recordReplace(target: "counter.name", value: "qa") },
            { $0.recordScroll(target: "counter.bottom", container: "counter.list", direction: .backward) },
            { $0.recordReplace(target: "counter.add", value: "QA") },
            { $0.recordTap(target: "counter.add\n") }
        ]
        for operation in operations {
            let recorder = ReproofRecorder(configuration: config())
            recorder.startIfNeeded(); XCTAssertEqual(operation(recorder), .rejected)
            failed(finish(recorder)); try assertNotFinalized()
            try FileManager.default.removeItem(at: root)
        }
    }

    func testEventCountBoundAndInterruptionFailClosed() throws {
        let recorder = ReproofRecorder(configuration: config(limits: CaptureLimits(maxEvents: 1)))
        recorder.startIfNeeded()
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .accepted)
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .rejected)
        failed(finish(recorder)); try assertNotFinalized()
        let capture = try json(root.appendingPathComponent("capture.json"))
        XCTAssertEqual(capture["endSequence"] as? Int, 1)
        XCTAssertEqual(capture["truncated"] as? Bool, true)
        XCTAssertEqual(capture["lostEvents"] as? Bool, true)
    }

    func testByteBoundFailsClosedWithoutSequenceGap() throws {
        let value = String(repeating: "a", count: 256)
        let p = CapturePolicy(screens: ["main"], replacementValues: ["input": [value]])
        let recorder = ReproofRecorder(configuration: config(policy: p, start: StartState(screen: "main", nodes: [:]),
            limits: CaptureLimits(maxBytes: 524_288)))
        recorder.startIfNeeded()
        var accepted = 0
        for _ in 0..<256 {
            if recorder.recordReplace(target: "input", value: value) == .accepted { accepted += 1 } else { break }
        }
        XCTAssertGreaterThan(accepted, 0); XCTAssertLessThan(accepted, 256)
        failed(finish(recorder)); try assertNotFinalized()
        XCTAssertEqual(try json(root.appendingPathComponent("capture.json"))["endSequence"] as? Int, accepted)
    }

    func testDurationBoundAndBackwardsClockFailClosed() throws {
        for end in [601.0, -1.0, Double.nan, Double.infinity] {
            let clock = LockedClock(0)
            var env = RecorderEnvironment.live; env.uptime = { clock.value }
            let recorder = ReproofRecorder(configuration: config(), environment: env)
            recorder.startIfNeeded(); clock.value = end
            XCTAssertEqual(recorder.recordTap(target: "counter.add"), .rejected)
            failed(finish(recorder)); try assertNotFinalized()
            try FileManager.default.removeItem(at: root)
        }
    }

    func testElapsedTimeCannotRegressBetweenAcceptedEvents() throws {
        let clock = LockedClock(0)
        var env = RecorderEnvironment.live; env.uptime = { clock.value }
        let recorder = ReproofRecorder(configuration: config(), environment: env)
        recorder.startIfNeeded(); clock.value = 2
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .accepted)
        clock.value = 1
        XCTAssertEqual(recorder.recordTap(target: "counter.add"), .rejected)
        failed(finish(recorder)); try assertNotFinalized()
    }

    func testFinishChecksElapsedTimeAfterQueueDrains() throws {
        let clock = LockedClock(0)
        var env = RecorderEnvironment.live; env.uptime = { clock.value }
        let recorder = ReproofRecorder(configuration: config(), environment: env)
        recorder.startIfNeeded(); recorder.recordTap(target: "counter.add")
        clock.value = 601
        failed(finish(recorder)); try assertNotFinalized()
    }

    func testLifecycleInterruption() throws {
        let recorder = ReproofRecorder(configuration: config())
        recorder.startIfNeeded(); recorder.markLifecycleInterrupted()
        failed(finish(recorder)); try assertNotFinalized()
    }

    func testConcurrentEventsKeepJSONLAndCaptureOrder() throws {
        let recorder = ReproofRecorder(configuration: config())
        recorder.startIfNeeded()
        DispatchQueue.concurrentPerform(iterations: 128) { _ in
            XCTAssertEqual(recorder.recordTap(target: "counter.add"), .accepted)
        }
        let paths = try exported(recorder)
        let events = try XCTUnwrap(json(paths.capture)["events"] as? [[String: Any]])
        let lines = try String(contentsOf: paths.events, encoding: .utf8).split(separator: "\n")
        XCTAssertEqual(events.count, 128); XCTAssertEqual(lines.count, 128)
        for (index, line) in lines.enumerated() {
            XCTAssertEqual(events[index]["seq"] as? Int, index + 1)
            let event = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
            XCTAssertEqual(event?["seq"] as? Int, index + 1)
        }
    }

    func testPendingFinishRejectsRepeatedCallbacksWithoutRetainingThem() throws {
        let entered = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        var env = RecorderEnvironment.live
        let create = env.createDirectory
        env.createDirectory = { url in entered.signal(); _ = release.wait(timeout: .now() + 30); try create(url) }
        let recorder = ReproofRecorder(configuration: config(), environment: env)
        recorder.startIfNeeded()
        XCTAssertEqual(entered.wait(timeout: .now() + 5), .success)
        let first = expectation(description: "first finish")
        let firstCount = LockedCounter()
        recorder.finish { result in
            if case .finished = result {} else { XCTFail("First finish failed") }
            firstCount.increment(); first.fulfill()
        }
        let count = LockedCounter()
        DispatchQueue.concurrentPerform(iterations: 1000) { _ in
            recorder.finish { result in
                if case .inactive = result {} else { XCTFail("Pending repeat retained") }
                count.increment()
            }
        }
        XCTAssertEqual(count.value, 1000)
        XCTAssertEqual(firstCount.value, 0)
        release.signal(); wait(for: [first], timeout: 5)
        XCTAssertEqual(firstCount.value, 1)
        let reentrant = expectation(description: "completed reentrant finish")
        recorder.finish { result in
            if case .finished = result {} else { XCTFail("Completed result missing") }
            recorder.finish { _ in reentrant.fulfill() }
        }
        wait(for: [reentrant], timeout: 5)
    }

    func testRecordFinishRaceExportsAllAndOnlyAcceptedEvents() throws {
        let recorder = ReproofRecorder(configuration: config())
        recorder.startIfNeeded()
        let accepted = LockedCounter()
        let finalized = expectation(description: "racing finish")
        DispatchQueue.concurrentPerform(iterations: 129) { index in
            if index == 64 { recorder.finish { _ in finalized.fulfill() } }
            else if recorder.recordTap(target: "counter.add") == .accepted { accepted.increment() }
        }
        wait(for: [finalized], timeout: 5)
        let paths = try exported(recorder)
        XCTAssertEqual(try json(paths.capture)["endSequence"] as? Int, accepted.value)
        XCTAssertEqual(try String(contentsOf: paths.events, encoding: .utf8).split(separator: "\n").count, accepted.value)
    }

    func testAcceptedWritesAndCompletionSurviveCallerReleasingRecorder() throws {
        let entered = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        var env = RecorderEnvironment.live
        let create = env.createDirectory
        env.createDirectory = { url in entered.signal(); _ = release.wait(timeout: .now() + 30); try create(url) }
        var recorder: ReproofRecorder? = ReproofRecorder(configuration: config(), environment: env)
        weak var weakRecorder = recorder
        recorder!.startIfNeeded()
        XCTAssertEqual(entered.wait(timeout: .now() + 5), .success)
        recorder!.recordTap(target: "counter.add")
        let done = expectation(description: "released recorder finished")
        recorder!.finish { result in
            if case let .finished(paths) = result {
                XCTAssertEqual((try? self.json(paths.capture)["endSequence"] as? Int), 1)
            } else { XCTFail("Released recorder lost work") }
            done.fulfill()
        }
        recorder = nil
        XCTAssertNotNil(weakRecorder)
        release.signal()
        wait(for: [done], timeout: 5)
        // No assertion about immediate deinit: the executing command may still be returning.
    }

    func testAcceptedWritesSurviveReleaseEvenWithoutFinish() throws {
        let appended = expectation(description: "accepted append survived")
        var env = RecorderEnvironment.live
        let append = env.append
        env.append = { data, url in try append(data, url); appended.fulfill() }
        var recorder: ReproofRecorder? = ReproofRecorder(configuration: config(), environment: env)
        recorder!.startIfNeeded(); recorder!.recordTap(target: "counter.add"); recorder = nil
        wait(for: [appended], timeout: 5)
        try assertNotFinalized()
    }

    func testFailuresAtEachWriteLeaveNoFinalizedPair() throws {
        // initialization metadata, stream, root capture, session capture, marker, final metadata.
        for failure in 1...6 {
            var env = RecorderEnvironment.live
            let write = env.write
            let count = LockedCounter()
            env.write = { data, url in
                count.increment()
                if count.value == failure { throw NSError(domain: "synthetic-test", code: failure) }
                try write(data, url)
            }
            let recorder = ReproofRecorder(configuration: config(), environment: env)
            recorder.startIfNeeded(); recorder.recordTap(target: "counter.add")
            failed(finish(recorder)); try assertNotFinalized()
            try FileManager.default.removeItem(at: root)
        }
    }

    func testUncertainFinalMetadataWriteIsInvalidated() throws {
        var env = RecorderEnvironment.live
        let write = env.write
        env.write = { data, url in
            try write(data, url)
            if url.lastPathComponent == "metadata.json",
               let object = try JSONSerialization.jsonObject(with: data) as? [String: Any], object["finalized"] as? Bool == true {
                throw NSError(domain: "synthetic-test", code: 1)
            }
        }
        let recorder = ReproofRecorder(configuration: config(), environment: env)
        recorder.startIfNeeded(); recorder.recordTap(target: "counter.add")
        failed(finish(recorder)); try assertNotFinalized()
    }

    func testAppendFailureAndDirectoryFailureCompleteOnce() throws {
        var env = RecorderEnvironment.live
        env.append = { _, _ in throw NSError(domain: "synthetic-test", code: 1) }
        let recorder = ReproofRecorder(configuration: config(), environment: env)
        recorder.startIfNeeded(); recorder.recordTap(target: "counter.add")
        failed(finish(recorder)); try assertNotFinalized()
        try FileManager.default.removeItem(at: root)
        env = .live
        env.createDirectory = { _ in throw NSError(domain: "synthetic-test", code: 1) }
        let unavailable = ReproofRecorder(configuration: config(), environment: env)
        unavailable.startIfNeeded(); failed(finish(unavailable))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.path))
    }

    func testRootIsNotUsedAsReturnedSessionIdentity() throws {
        let first = ReproofRecorder(configuration: config())
        first.startIfNeeded(); let firstPaths = try exported(first)
        let second = ReproofRecorder(configuration: config())
        second.startIfNeeded(); let secondPaths = try exported(second)
        XCTAssertNotEqual(firstPaths.directory, secondPaths.directory)
        XCTAssertNotEqual(try Data(contentsOf: firstPaths.capture), try Data(contentsOf: secondPaths.capture))
        XCTAssertEqual(try Data(contentsOf: secondPaths.capture), try Data(contentsOf: root.appendingPathComponent("capture.json")))
        XCTAssertEqual(try json(firstPaths.metadata)["finalized"] as? Bool, true)
    }
}

private final class LockedCounter {
    private let lock = NSLock()
    private var count = 0
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
    func increment() { lock.lock(); count += 1; lock.unlock() }
}
private final class LockedClock {
    private let lock = NSLock()
    private var stored: Double
    init(_ value: Double) { stored = value }
    var value: Double {
        get { lock.lock(); defer { lock.unlock() }; return stored }
        set { lock.lock(); stored = newValue; lock.unlock() }
    }
}
