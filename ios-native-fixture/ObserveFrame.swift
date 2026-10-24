import Foundation
import Vision

// Independent read-only pixel observer, not an input or semantic locator adapter.
// stderr is a fixed stage/timing protocol, never OCR text, paths or error prose.
enum ObserverStage: String {
    case started, invalidArguments = "invalid_arguments", imageRead = "image_read"
    case visionStarted = "vision_started", visionCompleted = "vision_completed"
    case outputWritten = "output_written", failed
}
let started = ProcessInfo.processInfo.systemUptime
func report(_ stage: ObserverStage) {
    let elapsed = max(0, Int((ProcessInfo.processInfo.systemUptime - started) * 1000))
    let line = "reproof_observer stage=\(stage.rawValue) elapsed_ms=\(elapsed)\n"
    FileHandle.standardError.write(Data(line.utf8))
}
report(.started)
guard CommandLine.arguments.count == 2 else {
    report(.invalidArguments)
    exit(2)
}
do {
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    // Report the byte read separately from Vision processing.
    let image = try Data(contentsOf: url)
    report(.imageRead)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["en-US"]
    report(.visionStarted)
    try VNImageRequestHandler(data: image, options: [:]).perform([request])
    report(.visionCompleted)
    let words = request.results?.compactMap { item -> [String: Any]? in
        guard let candidate = item.topCandidates(1).first else { return nil }
        return ["text": candidate.string, "confidence": candidate.confidence]
    } ?? []
    let body = try JSONSerialization.data(withJSONObject: ["observations": words], options: [.sortedKeys])
    FileHandle.standardOutput.write(body)
    report(.outputWritten)
} catch {
    report(.failed)
    exit(1)
}
