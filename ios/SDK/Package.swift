// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ReproofSDK",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "ReproofSDK", targets: ["ReproofSDK"])],
    dependencies: [],
    targets: [
        .target(name: "ReproofSDK"),
        .testTarget(name: "ReproofSDKTests", dependencies: ["ReproofSDK"])
    ]
)
