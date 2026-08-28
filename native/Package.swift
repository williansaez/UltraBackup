// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "UltraBackupApp",
    platforms: [
        .macOS(.v13)
    ],
    dependencies: [
        // Pinned to the minor series that was actually validated. This code
        // binds to API that only exists in recent 1.x (TerminalOptions.default
        // with regionalIndicatorWidth/initialBidiState, Color(red8:green8:blue8:)
        // and the exact 4-method LocalProcessTerminalViewDelegate shape), and
        // it is the terminal emulator that hosts an FDA-privileged engine — an
        // open `from: "1.2.0"` range let any 1.x land here.
        // build_app.sh additionally builds with --disable-automatic-resolution
        // so Package.resolved is authoritative.
        .package(
            url: "https://github.com/migueldeicaza/SwiftTerm.git",
            .upToNextMinor(from: "1.20.0")
        )
    ],
    targets: [
        .executableTarget(
            name: "UltraBackupApp",
            dependencies: [
                .product(name: "SwiftTerm", package: "SwiftTerm")
            ],
            path: "Sources/UltraBackupApp"
        )
    ]
)
