// Appended to the exact Foundation helpers extracted from the production template.
// No UIKit, simulator, application installation, or device execution is involved.
let scenario = CommandLine.arguments[1]
let output = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
let base = output.appendingPathComponent("current", isDirectory: true)
let session = "22222222-2222-4222-8222-222222222222"
let run = "11111111-1111-4111-8111-111111111111"
let profile = String(repeating: "a", count: 64)
let journalURL = base.appendingPathComponent("app-logs/\(session)/app-log.json")
let markerURL = base.appendingPathComponent("app-log-session.json")
let publication = RLAppLogPublication(base: base, applicationID: "io.reproof.sample.ios",
                                     runID: run, sessionID: session,
                                     profileDigest: profile, startedAtMs: 1000)
var gate = RLAppLogWriteGate()
var retry = RLAppLogRetryPolicy()
var operations: [String] = []
var attempts = 0

func events(_ count: Int) -> [[String: Any]] {
    (0..<count).map { index in
        ["seq": index + 1, "elapsedMs": index, "type": "lifecycle", "name": "active",
         "component": "application", "componentId": "app", "target": NSNull()]
    }
}

func diskWrite(_ data: Data, _ url: URL) throws {
    // The production helper invokes the recorder's unchanged durable writer.
    // This fixture uses the same Foundation file operations around failure injection.
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                           withIntermediateDirectories: true)
    try data.write(to: url, options: [.atomic])
    let handle = try FileHandle(forReadingFrom: url)
    try handle.synchronize()
    try handle.close()
}

func attempt(failure: String? = nil, count: Int = 1, truncated: Bool = false,
             maxBytes: Int = 1024 * 1024) -> Bool {
    precondition(retry.canSchedule())
    precondition(gate.begin())
    retry.beginAttempt()
    attempts += 1
    let capturedLoss = gate.lostEvents
    // A pending startup must block snapshot construction both during I/O and
    // after I/O returns, until its main-owner completion has recorded the result.
    precondition(!gate.begin())
    let success: Bool
    do {
        try publication.write(events: events(count), truncated: truncated,
                              lostEvents: capturedLoss, maxBytes: maxBytes,
                              createDirectory: { url in
                                  operations.append("directory")
                                  if failure == "directory" { throw NSError(domain: "injected", code: 1) }
                                  try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
                              },
                              writeDurably: { data, url in
                                  let phase = url.lastPathComponent == "app-log.json" ? "journal" : "marker"
                                  operations.append(phase)
                                  if failure == phase { throw NSError(domain: "injected", code: 2) }
                                  try diskWrite(data, url)
                              })
        success = true
    } catch {
        success = false
    }
    precondition(!gate.begin())
    let lossChanged = gate.finish(success: success)
    retry.finished(success: success)
    if lossChanged { retry.changed() }
    precondition(!gate.writing)
    if !success { precondition(gate.lostEvents) }
    return success
}

func preserveBeforeRecovery() throws {
    let before = output.appendingPathComponent("before", isDirectory: true)
    if FileManager.default.fileExists(atPath: base.path) {
        try FileManager.default.copyItem(at: base, to: before)
    } else {
        try FileManager.default.createDirectory(at: before, withIntermediateDirectories: true)
    }
}

switch scenario {
case "retry":
    retry.changed()
    for _ in 0..<3 {
        precondition(retry.canSchedule())
        retry.beginAttempt()
        retry.finished(success: false)
    }
    precondition(!retry.canSchedule())
    // New event or loss/truncation status each permits a new bounded burst.
    for _ in 0..<3 {
        retry.changed()
        precondition(retry.canSchedule())
        retry.beginAttempt()
        retry.finished(success: true)
        precondition(retry.failures == 0)
        for _ in 0..<3 {
            precondition(retry.canSchedule())
            retry.beginAttempt()
            retry.finished(success: false)
        }
        precondition(!retry.canSchedule())
    }
case "gate":
    precondition(gate.begin())
    precondition(!gate.lostEvents)
    precondition(!gate.begin())
    precondition(gate.finish(success: false))
    precondition(gate.lostEvents && !gate.writing)
    precondition(gate.begin())
    precondition(gate.lostEvents)
    precondition(!gate.finish(success: true))
    precondition(gate.lostEvents && !gate.writing)
    precondition(!gate.markLost())
    var explicitLoss = RLAppLogWriteGate()
    precondition(explicitLoss.begin())
    precondition(explicitLoss.markLost())
    precondition(!explicitLoss.finish(success: true))
    precondition(explicitLoss.lostEvents)
case "initial":
    precondition(attempt(count: 0))
    precondition(!gate.lostEvents)
    precondition(operations == ["directory", "journal", "marker"])
case "directory", "journal", "marker", "stale-marker":
    if scenario == "stale-marker" {
        let stale = RLAppLogPublication(base: base, applicationID: "io.reproof.sample.ios",
                                       runID: "33333333-3333-4333-8333-333333333333",
                                       sessionID: "44444444-4444-4444-8444-444444444444",
                                       profileDigest: profile, startedAtMs: 900)
        try stale.write(events: [], truncated: false, lostEvents: false,
                        maxBytes: 1024 * 1024, writeDurably: diskWrite)
    }
    let phase = scenario == "stale-marker" ? "marker" : scenario
    precondition(!attempt(failure: phase))
    let expected = phase == "directory" ? ["directory"] :
        phase == "journal" ? ["directory", "journal"] : ["directory", "journal", "marker"]
    precondition(operations == expected)
    try preserveBeforeRecovery()
    operations.removeAll()
    precondition(attempt(count: 2))
    precondition(gate.lostEvents)
    precondition(operations == ["directory", "journal", "marker"])
    // A later successful publication must keep loss sticky and refresh both files.
    operations.removeAll()
    precondition(attempt(count: 3))
    precondition(gate.lostEvents)
    precondition(operations == ["directory", "journal", "marker"])
case "permanent-directory", "permanent-journal", "permanent-marker":
    let phase = String(scenario.dropFirst("permanent-".count))
    for burst in 0..<3 {
        if burst > 0 { retry.changed() }
        for _ in 0..<3 { precondition(!attempt(failure: phase)) }
        precondition(retry.failures == 3)
        // Finish/flush requests without a new event or status revision cannot
        // restart I/O indefinitely, even though the last snapshot is dirty.
        for _ in 0..<1000 { precondition(!retry.canSchedule()) }
    }
    precondition(attempts == 9 && gate.lostEvents)
    precondition(!FileManager.default.fileExists(atPath: markerURL.path))
case "identity":
    var selectedRun = run
    var selectedProfile = profile
    var selectedSession = session
    var selectedApplication = "io.reproof.sample.ios"
    var selectedStart = 1000
    let frozen = RLAppLogPublication(base: base, applicationID: selectedApplication,
                                    runID: selectedRun, sessionID: selectedSession,
                                    profileDigest: selectedProfile, startedAtMs: selectedStart)
    selectedRun = "33333333-3333-4333-8333-333333333333"
    selectedProfile = String(repeating: "b", count: 64)
    selectedSession = "44444444-4444-4444-8444-444444444444"
    selectedApplication = "io.reproof.changed"
    selectedStart = 2000
    precondition(selectedRun != run && selectedProfile != profile && selectedSession != session)
    precondition(selectedApplication != "io.reproof.sample.ios" && selectedStart != 1000)
    try frozen.write(events: events(1), truncated: false, lostEvents: true,
                     maxBytes: 1024 * 1024, writeDurably: diskWrite)
case "bounds":
    precondition(attempt(count: 2000, truncated: true))
    let oldJournal = try Data(contentsOf: journalURL)
    let oldMarker = try Data(contentsOf: markerURL)
    operations.removeAll()
    // Reject over-budget bytes before either file or even the directory changes.
    precondition(!attempt(count: 2000, truncated: true, maxBytes: oldJournal.count - 1))
    precondition(operations.isEmpty)
    let unchangedJournal = try Data(contentsOf: journalURL)
    let unchangedMarker = try Data(contentsOf: markerURL)
    precondition(unchangedJournal == oldJournal)
    precondition(unchangedMarker == oldMarker)
    precondition(attempt(count: 2000, truncated: true))
    precondition(gate.lostEvents)
case "exact-byte-limit":
    precondition(attempt(count: 1))
    let length = try Data(contentsOf: journalURL).count
    precondition(attempt(count: 1, maxBytes: length))
    precondition(!gate.lostEvents)
default:
    fatalError("Unknown test scenario")
}
let result: [String: Any] = ["scenario": scenario, "attempts": attempts,
                             "operations": operations, "lostEvents": gate.lostEvents]
let resultData = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(decoding: resultData, as: UTF8.self))
