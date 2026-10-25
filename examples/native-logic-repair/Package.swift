// swift-tools-version: 5.9
import PackageDescription

// Supervisor-owned host test recipe, not an iOS application or a new oracle.
let package = Package(
    name: "ReproofNativeLogic",
    platforms: [.macOS(.v13)],
    dependencies: [],
    targets: [
        .target(name: "ReproSample", path: "Sources", sources: [
            "CounterLogic.swift", "SubmissionLogic.swift", "ResetLogic.swift"
        ]),
        .testTarget(name: "ReproLogicTests", dependencies: ["ReproSample"],
                    path: "Tests", sources: ["LogicTests.swift"])
    ]
)
