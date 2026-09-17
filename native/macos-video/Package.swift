// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ReproVideo",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "ReproVideo", targets: ["ReproVideo"]),
    ],
    targets: [
        .executableTarget(name: "ReproVideo"),
    ]
)
