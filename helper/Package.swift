// swift-tools-version:5.9
//
// The helper is built from this package (see packaging/build_mac.sh, gated
// behind SALVAGE_BUILD_HELPER=1). The auth-critical logic lives in
// SalvageHelperCore so it can be unit-tested without root or a real daemon:
//
//   swift test --package-path helper
//
import PackageDescription

let package = Package(
    name: "SalvageHelper",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "SalvageHelper", targets: ["SalvageHelper"]),
        .executable(name: "salvage-register", targets: ["SalvageRegister"]),
    ],
    targets: [
        .target(name: "SalvageHelperCore", path: "SalvageHelperCore"),
        .executableTarget(
            name: "SalvageHelper",
            dependencies: ["SalvageHelperCore"],
            path: "SalvageHelper"
        ),
        .executableTarget(name: "SalvageRegister", path: "salvage-register"),
        .testTarget(
            name: "SalvageHelperCoreTests",
            dependencies: ["SalvageHelperCore"],
            path: "Tests/SalvageHelperCoreTests"
        ),
    ]
)
