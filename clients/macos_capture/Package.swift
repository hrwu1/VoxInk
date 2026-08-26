// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "VoxInkMacOSCapture",
    platforms: [.macOS(.v13)],
    products: [
        .library(name: "VoxInkCapture", targets: ["VoxInkCapture"]),
        .executable(name: "voxink-capture", targets: ["VoxInkCaptureCLI"]),
    ],
    targets: [
        .target(name: "VoxInkCapture"),
        .executableTarget(
            name: "VoxInkCaptureCLI",
            dependencies: ["VoxInkCapture"]
        ),
        .testTarget(
            name: "VoxInkCaptureTests",
            dependencies: ["VoxInkCapture"]
        ),
    ],
    swiftLanguageModes: [.v5]
)
