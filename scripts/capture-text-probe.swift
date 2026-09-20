import Foundation
import Vision

// Sample-only capture diagnostic. Emit only expected fixture text presence.
let directory = URL(fileURLWithPath: CommandLine.arguments[1])
var results: [[String: Any]] = []
for url in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil).sorted(by: { $0.path < $1.path }) {
    guard ["jpg", "jpeg", "png"].contains(url.pathExtension) else { continue }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(url: url).perform([request])
    let words = request.results?.compactMap { $0.topCandidates(1).first?.string } ?? []
    let countRequest = VNRecognizeTextRequest()
    countRequest.recognitionLevel = .accurate
    countRequest.usesLanguageCorrection = false
    countRequest.minimumTextHeight = 0.003
    countRequest.customWords = ["0", "1", "2"]
    countRequest.regionOfInterest = CGRect(x: 0.4, y: 0.62, width: 0.2, height: 0.09)
    try VNImageRequestHandler(url: url).perform([countRequest])
    let counts = countRequest.results?.compactMap { observation -> String? in
        guard let word = observation.topCandidates(1).first,
              word.confidence >= 0.7, ["0", "1", "2"].contains(word.string) else { return nil }
        return word.string
    } ?? []
    results.append(["file": url.lastPathComponent,
                    "counter": words.contains("Counter"),
                    "name": words.contains("QA"),
                    "count": words.contains("2"),
                    "nav": words.contains("Next") ? "Next" : "unknown",
                    "observedCount": counts.count == 1 ? counts[0] : "unknown"])
}
let encoded = try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
print(String(data: encoded, encoding: .utf8)!)
