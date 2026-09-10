// swift-tools-version: 6.0

import PackageDescription

// The menu-bar control surface. It drives the helper's HTTP API like
// `voxink_cli` and `voxink_mcp` and captures no audio; the capture package is
// a dependency only for the floating caption panel it shows.
let package = Package(
    name: "VoxInkMenuBar",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "voxink-menubar", targets: ["VoxInkMenuBar"]),
    ],
    dependencies: [
        .package(path: "../clients/macos_capture"),
    ],
    targets: [
        .executableTarget(
            name: "VoxInkMenuBar",
            dependencies: [
                .product(name: "VoxInkCapture", package: "macos_capture"),
            ]
        ),
        .testTarget(
            name: "VoxInkMenuBarTests",
            dependencies: ["VoxInkMenuBar"]
        ),
    ],
    swiftLanguageModes: [.v5]
)
