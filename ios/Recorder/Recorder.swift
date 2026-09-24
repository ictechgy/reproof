import Foundation

final class Recorder {
    static let shared = Recorder()

    private struct Event: Codable {
        let id: String
        let seq: Int
        let action: String
        let target: String
        let parameters: [String: String]
        let elapsedMs: Int?
    }

    private struct Capture: Codable {
        let schemaVersion: Int
        let platform: String
        let applicationId: String
        let sessionId: String
        let fixture: Fixture
        let startState: StartState
        let events: [Event]
        let truncated: Bool
        let lostEvents: Bool
        let endSequence: Int
        let startedAtMs: Int
    }

    private struct Fixture: Codable {
        let id: String
        let version: Int
        let inputs: [String: String]
    }

    private struct StartState: Codable {
        let screen: String
        let nodes: [String: String]
    }

    private let stateLock = NSLock()
    private let writerQueue = DispatchQueue(label: "io.reproof.recorder.writer", qos: .utility)
    private var started = false
    private var completed = false
    private var finishing = false
    private var sequence = 0
    private var events: [Event] = []
    private var lostEvents = false
    private var truncated = false
    private var eventBytes = 0
    private var sessionID = ""
    private var startedAtMs = 0
    private var sessionStartUptime = 0.0
    private var jsonlURL: URL?
    private var sessionDirectory: URL?

    private let maxDurationMs = 10 * 60 * 1000
    private let maxBytes = 20 * 1024 * 1024
    // Reserve metadata, both final capture copies, and their atomic temporary copies.
    private let metadataBudget = 64 * 1024
    private let captureOverhead = 64 * 1024

    private init() {}

    func startIfNeeded() {
        stateLock.lock()
        guard !started, !finishing, !completed else {
            stateLock.unlock()
            return
        }
        let wallNow = Int(Date().timeIntervalSince1970 * 1000)
        let uptimeNow = ProcessInfo.processInfo.systemUptime
        let newSessionID = UUID().uuidString.lowercased()
        let base = applicationSupportDirectory()
        let directory = base.appendingPathComponent(newSessionID, isDirectory: true)
        let eventsURL = directory.appendingPathComponent("events.jsonl")

        sessionID = newSessionID
        startedAtMs = wallNow
        sessionStartUptime = uptimeNow
        sequence = 0
        events = []
        lostEvents = false
        truncated = false
        eventBytes = 0
        sessionDirectory = directory
        jsonlURL = eventsURL
        started = true

        // The metadata command is enqueued while holding the same lock as event commands.
        // The serial writer therefore always creates the stream before appending events.
        writerQueue.async { [weak self] in
            guard let self else { return }
            do {
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                try self.writeMetadata(
                    to: directory.appendingPathComponent("metadata.json"),
                    sessionID: newSessionID,
                    startedAtMs: wallNow,
                    finalized: false,
                    endSequence: nil
                )
                try Data().write(to: eventsURL, options: .atomic)
            } catch {
                self.markWriteFailure()
            }
        }
        stateLock.unlock()
    }

    func recordTap(target: String) {
        enqueue(action: "tap", target: target, parameters: [:])
    }

    func recordReplace(target: String, value: String) {
        guard target == "counter.name", isAllowedValue(value) else {
            invalidateWithoutRawValue()
            return
        }
        enqueue(action: "replace", target: target, parameters: ["value": value])
    }

    func recordScroll(target: String, container: String) {
        guard target == "counter.bottom", container == "counter.list" else {
            invalidateWithoutRawValue()
            return
        }
        enqueue(action: "scroll_to", target: target, parameters: [
            "container": container,
            "direction": "forward"
        ])
    }

    func recordBack(target: String) {
        guard target == "counter.back" else {
            invalidateWithoutRawValue()
            return
        }
        enqueue(action: "navigate_back", target: target, parameters: [:])
    }

    func markLifecycleInterrupted() {
        stateLock.lock()
        guard started, !finishing else {
            stateLock.unlock()
            return
        }
        truncated = true
        lostEvents = true
        stateLock.unlock()
    }

    func finish(completion: ((Bool) -> Void)? = nil) {
        stateLock.lock()
        guard started, !finishing else {
            stateLock.unlock()
            completion?(false)
            return
        }
        if elapsedMs(now: ProcessInfo.processInfo.systemUptime) > maxDurationMs {
            truncated = true
            lostEvents = true
        }
        finishing = true
        let directory = sessionDirectory
        let eventsURL = jsonlURL
        let finalSessionID = sessionID
        let finalStartedAtMs = startedAtMs

        // This is deliberately async. It is enqueued before releasing the state lock,
        // after every accepted event enqueue, so all writes and failures are observed
        // before the final state is read.
        writerQueue.async { [weak self] in
            self?.finalize(
                directory: directory,
                eventsURL: eventsURL,
                sessionID: finalSessionID,
                startedAtMs: finalStartedAtMs,
                completion: completion
            )
        }
        stateLock.unlock()
    }

    private func enqueue(action: String, target: String, parameters: [String: String]) {
        stateLock.lock()
        guard started, !finishing, !truncated, isValidEvent(action: action, target: target, parameters: parameters) else {
            if started, !finishing { lostEvents = true; truncated = true }
            stateLock.unlock()
            return
        }

        let now = ProcessInfo.processInfo.systemUptime
        let elapsed = elapsedMs(now: now)
        if elapsed > maxDurationMs {
            truncated = true
            lostEvents = true
            stateLock.unlock()
            return
        }

        let nextSequence = sequence + 1
        let event = Event(
            id: "e\(nextSequence)",
            seq: nextSequence,
            action: action,
            target: target,
            parameters: parameters,
            elapsedMs: elapsed
        )
        let lineData: Data
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            lineData = try encoder.encode(event) + Data([10])
        } catch {
            lostEvents = true
            truncated = true
            stateLock.unlock()
            return
        }

        let newEventBytes = eventBytes + lineData.count
        guard reservedBytes(forEventBytes: newEventBytes) <= maxBytes else {
            truncated = true
            lostEvents = true
            stateLock.unlock()
            return
        }

        sequence = nextSequence
        eventBytes = newEventBytes
        events.append(event)
        let eventsURL = jsonlURL
        // Keep sequence assignment, event append, and writer enqueue in one boundary.
        writerQueue.async { [weak self] in
            guard let self, let eventsURL else { return }
            do {
                let handle = try FileHandle(forWritingTo: eventsURL)
                try handle.seekToEnd()
                try handle.write(contentsOf: lineData)
                try handle.close()
            } catch {
                self.markWriteFailure()
            }
        }
        stateLock.unlock()
    }

    private func finalize(
        directory: URL?,
        eventsURL: URL?,
        sessionID: String,
        startedAtMs: Int,
        completion: ((Bool) -> Void)?
    ) {
        guard let directory else {
            finishState(success: false, completion: completion)
            return
        }

        stateLock.lock()
        if elapsedMs(now: ProcessInfo.processInfo.systemUptime) > maxDurationMs {
            truncated = true
            lostEvents = true
        }
        let finalEvents = events
        let finalSequence = sequence
        let finalTruncated = truncated
        let finalLostEvents = lostEvents
        stateLock.unlock()

        var success = false
        var valid = false
        do {
            let capture = Capture(
                schemaVersion: 2,
                platform: "ios",
                applicationId: "io.reproof.sample.ios",
                sessionId: sessionID,
                fixture: Fixture(id: ReproCase.current.fixtureID, version: 1, inputs: [:]),
                startState: StartState(screen: "main", nodes: ["counter.count": "0", "counter.name": ""]),
                events: finalEvents,
                truncated: finalTruncated,
                lostEvents: finalLostEvents,
                endSequence: finalSequence,
                startedAtMs: startedAtMs
            )
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let data = try encoder.encode(capture)
            let marker = Data("{\"finalized\":true,\"endSequence\":\(finalSequence)}\n".utf8)
            let estimatedBytes = data.count * 4 + metadataBudget + (eventsURL == nil ? 0 : data.count)
            guard estimatedBytes <= maxBytes else {
                markWriteFailure()
                return finishState(success: false, completion: completion)
            }
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try data.write(to: applicationSupportDirectory().appendingPathComponent("capture.json"), options: .atomic)
            try data.write(to: directory.appendingPathComponent("capture.json"), options: .atomic)
            try marker.write(to: directory.appendingPathComponent("finalized.json"), options: .atomic)
            success = true

            stateLock.lock()
            let finalStateIsValid = !truncated && !lostEvents
            stateLock.unlock()
            guard finalStateIsValid else {
                return finishState(success: true, completion: completion)
            }
            try writeMetadata(
                to: directory.appendingPathComponent("metadata.json"),
                sessionID: sessionID,
                startedAtMs: startedAtMs,
                finalized: true,
                endSequence: finalSequence
            )
            valid = true
        } catch {
            markWriteFailure()
        }
        finishState(success: success, valid: valid, completion: completion)
    }

    private func finishState(success: Bool, valid: Bool = false, completion: ((Bool) -> Void)?) {
        stateLock.lock()
        started = false
        finishing = false
        completed = true
        events.removeAll(keepingCapacity: false)
        stateLock.unlock()
        completion?(success && valid)
    }

    private func writeMetadata(
        to url: URL,
        sessionID: String,
        startedAtMs: Int,
        finalized: Bool,
        endSequence: Int?
    ) throws {
        var metadata: [String: Any] = [
            "schemaVersion": 2,
            "platform": "ios",
            "applicationId": "io.reproof.sample.ios",
            "sessionId": sessionID,
            "fixture": ["id": ReproCase.current.fixtureID, "version": 1, "inputs": [:]],
            "startState": ["screen": "main", "nodes": ["counter.count": "0", "counter.name": ""]],
            "startedAtMs": startedAtMs,
            "finalized": finalized
        ]
        if let endSequence { metadata["endSequence"] = endSequence }
        let data = try JSONSerialization.data(withJSONObject: metadata, options: [.sortedKeys])
        try data.write(to: url, options: .atomic)
    }

    private func elapsedMs(now: TimeInterval) -> Int {
        max(0, Int((now - sessionStartUptime) * 1000.0))
    }

    private func reservedBytes(forEventBytes bytes: Int) -> Int {
        metadataBudget + bytes + ((captureOverhead + bytes) * 4)
    }

    private func isAllowedValue(_ value: String) -> Bool {
        value.isEmpty || value == "QA" || value == "Test"
    }

    private func isValidEvent(action: String, target: String, parameters: [String: String]) -> Bool {
        switch action {
        case "tap":
            let commonTargets = ["counter.add", "counter.next", "counter.back"]
            if target == "counter.reset" {
                return ReproCase.current == .reset && parameters.isEmpty
            }
            return commonTargets.contains(target) && parameters.isEmpty
        case "replace":
            return target == "counter.name" && parameters.count == 1 && isAllowedValue(parameters["value"] ?? "")
        case "scroll_to":
            return target == "counter.bottom" && parameters == ["container": "counter.list", "direction": "forward"]
        case "navigate_back":
            return target == "counter.back" && parameters.isEmpty
        default:
            return false
        }
    }

    private func invalidateWithoutRawValue() {
        stateLock.lock()
        if started, !finishing {
            lostEvents = true
            truncated = true
        }
        stateLock.unlock()
    }

    private func markWriteFailure() {
        stateLock.lock()
        if started {
            lostEvents = true
            truncated = true
        }
        stateLock.unlock()
    }

    private func applicationSupportDirectory() -> URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Reproof", isDirectory: true)
    }
}
