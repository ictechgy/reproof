import Foundation
import XCTest
import ReproofSDK

/// This file deliberately imports only the public API, without @testable.
final class PublicAPITests: XCTestCase {
    func testPublicConsumerCanConstructDisabledRecorderWithoutEffects() {
        let configuration = RecorderConfiguration(
            rootDirectory: URL(fileURLWithPath: "/unused-synthetic-sdk-test"),
            application: ApplicationMetadata(id: "example.app"),
            fixture: FixtureMetadata(id: "synthetic", version: 1),
            startState: StartState(screen: "main", nodes: [:]), policy: CapturePolicy())
        let recorder = ReproofRecorder(configuration: configuration)
        XCTAssertEqual(recorder.startIfNeeded(), .disabled)
    }
}
