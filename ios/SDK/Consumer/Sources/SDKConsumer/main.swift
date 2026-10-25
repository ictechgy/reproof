import Foundation
import ReproofSDK

// Independent compile-only consumer. No @testable, sample source, network or UI.
let recorder = ReproofRecorder(configuration: RecorderConfiguration(
    rootDirectory: URL(fileURLWithPath: "/unused-synthetic-consumer"),
    application: ApplicationMetadata(id: "example.inventory"),
    fixture: FixtureMetadata(id: "inventory", version: 1),
    startState: StartState(screen: "main", nodes: [:]),
    policy: CapturePolicy(screens: ["main"], tapTargets: ["inventory.save"])
))
// Deliberately do not start recording; CI compiles but does not run this consumer.
