// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SDKConsumer",
    platforms: [.macOS(.v13)],
    dependencies: [.package(name: "ReproofSDK", path: "..")],
    targets: [.executableTarget(name: "SDKConsumer", dependencies: [.product(name: "ReproofSDK", package: "ReproofSDK")])]
)
