// swift-tools-version:5.9
import PackageDescription

// A vendored, local-path package: allowed in 0.1 (no network needed).
let package = Package(
    name: "LocalPkg",
    products: [.library(name: "LocalPkg", targets: ["LocalPkg"])],
    targets: [.target(name: "LocalPkg")]
)
