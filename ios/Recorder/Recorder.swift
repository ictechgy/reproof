import Foundation
import ReproofSDK

/// Sample-only policy adapter. The reusable package has no dependency on ReproCase.
final class Recorder {
    static let shared = Recorder()
    private let recorder: ReproofRecorder

    private init() {
        let sampleCase = ReproCase.current
        var taps: Set<String> = ["counter.add", "counter.next", "counter.back"]
        if sampleCase == .reset { taps.insert("counter.reset") }
        let policy = CapturePolicy(
            screens: ["main"],
            startNodeValues: ["counter.count": ["0"], "counter.name": [""]],
            tapTargets: taps,
            replacementValues: ["counter.name": ["", "QA", "Test"]],
            scrollRules: [ScrollRule(target: "counter.bottom", container: "counter.list")],
            backTargets: ["counter.back"]
        )
        recorder = ReproofRecorder(configuration: RecorderConfiguration(
            rootDirectory: FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Reproof", isDirectory: true),
            application: ApplicationMetadata(id: "io.reproof.sample.ios"),
            fixture: FixtureMetadata(id: sampleCase.fixtureID, version: 1),
            startState: StartState(screen: "main", nodes: ["counter.count": "0", "counter.name": ""]),
            policy: policy
        ))
    }

    func startIfNeeded() { _ = recorder.startIfNeeded() }
    func recordTap(target: String) { _ = recorder.recordTap(target: target) }
    func recordReplace(target: String, value: String) { _ = recorder.recordReplace(target: target, value: value) }
    func recordScroll(target: String, container: String) { _ = recorder.recordScroll(target: target, container: container) }
    func recordBack(target: String) { _ = recorder.recordBack(target: target) }
    func markLifecycleInterrupted() { recorder.markLifecycleInterrupted() }
    func finish(completion: ((Bool) -> Void)? = nil) {
        recorder.finish { result in
            if case .finished = result { completion?(true) } else { completion?(false) }
        }
    }
}
