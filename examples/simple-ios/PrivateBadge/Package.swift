// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "PrivateBadge",
    platforms: [.iOS(.v17)],
    products: [.library(name: "PrivateBadge", targets: ["PrivateBadge"])],
    targets: [.target(name: "PrivateBadge")]
)
