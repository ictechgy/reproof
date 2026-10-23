import Foundation
import Vision

// Independent read-only pixel observer, not an input or semantic locator adapter.
guard CommandLine.arguments.count == 2 else { exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
try VNImageRequestHandler(url: url).perform([request])
let words = request.results?.compactMap { item -> [String: Any]? in
    guard let candidate = item.topCandidates(1).first else { return nil }
    return ["text": candidate.string, "confidence": candidate.confidence]
} ?? []
let body = try JSONSerialization.data(withJSONObject: ["observations": words], options: [.sortedKeys])
FileHandle.standardOutput.write(body)
