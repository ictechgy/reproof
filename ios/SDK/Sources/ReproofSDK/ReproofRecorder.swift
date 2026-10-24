import Foundation

public enum StartResult: Equatable { case started, alreadyStarted, disabled, finished }
public enum RecordResult: Equatable { case accepted, rejected, inactive }

/// Same-session files only. The compatibility root capture.json is not an export identity.
public struct SessionExport {
    public let directory: URL
    public let capture: URL
    public let metadata: URL
    public let marker: URL
    public let events: URL
    internal init(directory: URL) {
        self.directory = directory
        capture = directory.appendingPathComponent("capture.json")
        metadata = directory.appendingPathComponent("metadata.json")
        marker = directory.appendingPathComponent("finalized.json")
        events = directory.appendingPathComponent("events.jsonl")
    }
}

public enum FinishResult {
    case finished(SessionExport)
    case failed
    case inactive
}

// Test seams are internal, never a second writer or a public filesystem hook.
internal struct RecorderEnvironment {
    var wallTimeMs: () -> Int
    var uptime: () -> TimeInterval
    var sessionID: () -> String
    var createDirectory: (URL) throws -> Void
    var write: (Data, URL) throws -> Void
    var append: (Data, URL) throws -> Void
    var remove: (URL) throws -> Void
    static var live: RecorderEnvironment {
        RecorderEnvironment(
            wallTimeMs: { Int(Date().timeIntervalSince1970 * 1000) },
            uptime: { ProcessInfo.processInfo.systemUptime },
            sessionID: { UUID().uuidString.lowercased() },
            createDirectory: { try FileManager.default.createDirectory(at: $0, withIntermediateDirectories: true) },
            write: { try $0.write(to: $1, options: .atomic) },
            append: { data, url in
                let handle = try FileHandle(forWritingTo: url)
                do {
                    try handle.seekToEnd()
                    try handle.write(contentsOf: data)
                    try handle.close()
                } catch {
                    try? handle.close()
                    throw error
                }
            },
            remove: { try FileManager.default.removeItem(at: $0) }
        )
    }
}

/// Thread-safe, one-shot local annotation recorder. No UI hooks, network, logs,
/// input dispatch, replay authority, installed-build attestation or automatic start.
public final class ReproofRecorder {
    private struct Event: Codable {
        let id: String
        let seq: Int
        let action: String
        let target: String
        let parameters: [String: String]
        let elapsedMs: Int
    }
    private struct Capture: Encodable {
        let schemaVersion = 2
        let platform = "ios"
        let applicationId: String
        let sessionId: String
        let fixture: FixtureMetadata
        let startState: StartState
        let events: [Event]
        let truncated: Bool
        let lostEvents: Bool
        let endSequence: Int
        let startedAtMs: Int
    }
    private struct Metadata: Encodable {
        let schemaVersion = 2
        let platform = "ios"
        let applicationId: String
        let sessionId: String
        let fixture: FixtureMetadata
        let startState: StartState
        let startedAtMs: Int
        let finalized: Bool
        let endSequence: Int?
    }
    private enum State { case disabled, ready, recording, finishing, completed }
    private let stateLock = NSLock()
    private let configuration: RecorderConfiguration?
    private let environment: RecorderEnvironment?
    private let writerQueue: DispatchQueue?
    private var state: State
    private var sequence = 0
    private var events: [Event] = []
    private var lostEvents = false
    private var truncated = false
    private var eventBytes = 0
    private var lastElapsedMs = 0
    private var sessionID = ""
    private var startedAtMs = 0
    private var sessionStartUptime = 0.0
    private var export: SessionExport?
    private var completion: ((FinishResult) -> Void)?
    private var result: FinishResult = .inactive
    private let metadataBudget = 64 * 1024
    private let captureOverhead = 64 * 1024

    public convenience init(configuration: RecorderConfiguration) {
        self.init(configuration: configuration, environment: .live)
    }
    internal init(configuration: RecorderConfiguration, environment: RecorderEnvironment) {
        if Admission.configuration(configuration) {
            self.configuration = configuration; self.environment = environment
            self.writerQueue = DispatchQueue(label: "io.reproof.recorder.writer", qos: .utility)
            self.state = .ready
        } else {
            // Invalid data never becomes recorder state or a queued diagnostic.
            self.configuration = nil; self.environment = nil; self.writerQueue = nil
            self.state = .disabled
        }
    }

    /// Whether configuration was admitted. Completion does not reset this flag.
    public var isEnabled: Bool {
        stateLock.lock(); defer { stateLock.unlock() }
        return state != .disabled
    }

    @discardableResult public func startIfNeeded() -> StartResult {
        stateLock.lock()
        defer { stateLock.unlock() }
        switch state {
        case .disabled: return .disabled
        case .recording: return .alreadyStarted
        case .finishing, .completed: return .finished
        case .ready: break
        }
        guard let c = configuration, let env = environment, let queue = writerQueue else { return .disabled }
        let wall = env.wallTimeMs()
        let uptime = env.uptime()
        let id = env.sessionID()
        guard wall >= 0, uptime.isFinite, uptime >= 0, UUID(uuidString: id) != nil,
              id.utf8.count == 36, id == id.lowercased() else {
            state = .disabled
            return .disabled
        }
        let session = SessionExport(directory: c.rootDirectory.appendingPathComponent(id, isDirectory: true))
        sessionID = id; startedAtMs = wall; sessionStartUptime = uptime; export = session
        state = .recording
        // Strong capture retains accepted work even if the caller releases its recorder.
        // All command submissions share this lock, including initialization and finish.
        queue.async {
            do {
                try env.createDirectory(session.directory)
                try self.writeMetadata(finalized: false, endSequence: nil, to: session.metadata)
                try env.write(Data(), session.events)
            } catch { self.markWriteFailure() }
        }
        return .started
    }

    @discardableResult public func recordTap(target: String) -> RecordResult {
        record(action: "tap", target: target, parameters: [:])
    }
    @discardableResult public func recordReplace(target: String, value: String) -> RecordResult {
        // Do not capture an unadmitted value in a queue or an event.
        guard Admission.identifier(target), Admission.value(value) else { return reject() }
        return record(action: "replace", target: target, parameters: ["value": value])
    }
    @discardableResult public func recordScroll(target: String, container: String,
                                               direction: ScrollDirection = .forward) -> RecordResult {
        guard Admission.identifier(target), Admission.identifier(container) else { return reject() }
        return record(action: "scroll_to", target: target,
                      parameters: ["container": container, "direction": direction.rawValue])
    }
    @discardableResult public func recordBack(target: String) -> RecordResult {
        record(action: "navigate_back", target: target, parameters: [:])
    }
    public func markLifecycleInterrupted() { _ = reject() }

    /// Each supplied callback runs once outside the lock on an unspecified thread.
    /// A call while finalization is pending returns .inactive immediately, without
    /// retaining another callback. A completed call returns the cached result.
    public func finish(completion: ((FinishResult) -> Void)? = nil) {
        stateLock.lock()
        switch state {
        case .disabled, .ready:
            stateLock.unlock(); completion?(.inactive); return
        case .completed:
            let final = result
            stateLock.unlock(); completion?(final); return
        case .finishing:
            stateLock.unlock(); completion?(.inactive); return
        case .recording: break
        }
        self.completion = completion
        state = .finishing
        writerQueue!.async { self.finalize() }
        stateLock.unlock()
    }

    private func record(action: String, target: String, parameters: [String: String]) -> RecordResult {
        stateLock.lock()
        defer { stateLock.unlock() }
        guard state == .recording, let c = configuration, let env = environment,
              let session = export, let queue = writerQueue else { return .inactive }
        guard !truncated, Admission.identifier(target), admitted(action: action, target: target, parameters: parameters, policy: c.policy) else {
            invalidate(); return .rejected
        }
        guard let elapsed = elapsedMs(now: env.uptime()), elapsed >= lastElapsedMs, elapsed <= c.limits.maxDurationMs,
              sequence < c.limits.maxEvents else { invalidate(); return .rejected }
        let next = sequence + 1
        let event = Event(id: "e\(next)", seq: next, action: action, target: target, parameters: parameters, elapsedMs: elapsed)
        guard let encoded = try? encode(event) else { invalidate(); return .rejected }
        let line = encoded + Data([10])
        let newBytes = eventBytes + line.count
        guard metadataBudget + newBytes + ((captureOverhead + newBytes) * 4) <= c.limits.maxBytes else {
            invalidate(); return .rejected
        }
        sequence = next; eventBytes = newBytes; lastElapsedMs = elapsed; events.append(event)
        queue.async {
            do { try env.append(line, session.events) }
            catch { self.markWriteFailure() }
        }
        return .accepted
    }

    private func admitted(action: String, target: String, parameters: [String: String], policy: CapturePolicy) -> Bool {
        switch action {
        case "tap": return parameters.isEmpty && policy.tapTargets.contains(target)
        case "replace":
            guard parameters.count == 1, let value = parameters["value"], Admission.value(value) else { return false }
            return policy.replacementValues[target]?.contains(value) == true
        case "scroll_to":
            guard parameters.count == 2, let container = parameters["container"], Admission.identifier(container),
                  let raw = parameters["direction"], let direction = ScrollDirection(rawValue: raw) else { return false }
            return policy.scrollRules.contains(ScrollRule(target: target, container: container, direction: direction))
        case "navigate_back": return parameters.isEmpty && policy.backTargets.contains(target)
        default: return false
        }
    }
    private func reject() -> RecordResult {
        stateLock.lock(); defer { stateLock.unlock() }
        guard state == .recording else { return .inactive }
        invalidate(); return .rejected
    }
    private func invalidate() { lostEvents = true; truncated = true }
    private func markWriteFailure() {
        stateLock.lock(); invalidate(); stateLock.unlock()
    }
    private func elapsedMs(now: TimeInterval) -> Int? {
        let value = (now - sessionStartUptime) * 1000
        guard value.isFinite, value >= 0, value <= 600_001 else { return nil }
        return Int(value)
    }
    private func encode<T: Encodable>(_ value: T) throws -> Data {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(value)
    }
    private func writeMetadata(finalized: Bool, endSequence: Int?, to url: URL) throws {
        let c = configuration!
        let data = try encode(Metadata(applicationId: c.application.id, sessionId: sessionID, fixture: c.fixture,
            startState: c.startState, startedAtMs: startedAtMs, finalized: finalized, endSequence: endSequence))
        try environment!.write(data, url)
    }
    private func finalize() {
        guard let c = configuration, let env = environment, let session = export else {
            finishState(.failed); return
        }
        stateLock.lock()
        if let elapsed = elapsedMs(now: env.uptime()), elapsed >= lastElapsedMs, elapsed <= c.limits.maxDurationMs {} else { invalidate() }
        let capture = Capture(applicationId: c.application.id, sessionId: sessionID, fixture: c.fixture,
            startState: c.startState, events: events, truncated: truncated, lostEvents: lostEvents,
            endSequence: sequence, startedAtMs: startedAtMs)
        stateLock.unlock()
        do {
            let data = try encode(capture)
            guard data.count * 5 + metadataBudget <= c.limits.maxBytes else {
                markWriteFailure(); finishState(.failed); return
            }
            try env.write(data, c.rootDirectory.appendingPathComponent("capture.json"))
            try env.write(data, session.capture)
            guard !capture.truncated, !capture.lostEvents else { finishState(.failed); return }
            let marker = Data("{\"finalized\":true,\"endSequence\":\(capture.endSequence)}\n".utf8)
            try env.write(marker, session.marker)
            // The authoritative metadata commit is last. Earlier failures remain unfinalized.
            try writeMetadata(finalized: true, endSequence: capture.endSequence, to: session.metadata)
            finishState(.finished(session))
        } catch {
            markWriteFailure()
            // Best effort makes an uncertain final metadata write fail closed as well.
            try? env.remove(session.marker)
            try? writeMetadata(finalized: false, endSequence: nil, to: session.metadata)
            finishState(.failed)
        }
    }
    private func finishState(_ final: FinishResult) {
        stateLock.lock()
        result = final; state = .completed; events.removeAll(keepingCapacity: false)
        let callback = completion; completion = nil
        stateLock.unlock()
        callback?(final)
    }
}
