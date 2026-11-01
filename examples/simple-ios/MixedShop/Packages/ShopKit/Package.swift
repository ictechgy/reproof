// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "ShopKit",
    platforms: [.iOS(.v17)],
    products: [.library(name: "ShopKit", targets: ["ShopKit"])],
    targets: [
        .target(name: "ShopKit"),
        .testTarget(name: "ShopKitTests", dependencies: ["ShopKit"])
    ]
)
