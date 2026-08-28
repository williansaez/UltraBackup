//
//  TerminalHost.swift
//  UltraBackupApp
//
//  Hosts the Python engine (`python3 -m ultrabackup tui`) inside a pseudo
//  terminal owned by THIS app. Because the interpreter is a child of
//  UltraBackup.app, macOS attributes TCC (Full Disk Access) to UltraBackup —
//  which is the whole point of the native app: the FDA grant no longer has to
//  be handed to Terminal.app.
//

import AppKit
import Darwin
import Foundation
import SwiftTerm

/// Colors, font and geometry for the embedded terminal — a dark palette in the
/// same family as the curses TUI (background `#1e2430`, light foreground).
enum TerminalTheme {
    static let columns = 120
    static let rows = 34
    static let fontSize: CGFloat = 13

    /// #1e2430
    static let background = NSColor(srgbRed: 0x1e / 255.0, green: 0x24 / 255.0,
                                    blue: 0x30 / 255.0, alpha: 1.0)
    /// #d7dee9
    static let foreground = NSColor(srgbRed: 0xd7 / 255.0, green: 0xde / 255.0,
                                    blue: 0xe9 / 255.0, alpha: 1.0)
    /// #2f6f4f — selection highlight, readable over the dark background
    static let selection = NSColor(srgbRed: 0x2f / 255.0, green: 0x6f / 255.0,
                                   blue: 0x4f / 255.0, alpha: 1.0)
    /// #7ee787 — the TUI's prompt green, reused for the caret
    static let caret = NSColor(srgbRed: 0x7e / 255.0, green: 0xe7 / 255.0,
                               blue: 0x87 / 255.0, alpha: 1.0)

    /// SF Mono at `fontSize`. `monospacedSystemFont` yields SF Mono on stock
    /// macOS; on installs where it resolves to a proportional system face,
    /// Menlo is the safer fixed-pitch fallback (a terminal grid needs one).
    static var font: NSFont {
        let mono = NSFont.monospacedSystemFont(ofSize: fontSize, weight: .regular)
        if mono.isFixedPitch {
            return mono
        }
        return NSFont(name: "Menlo", size: fontSize) ?? mono
    }

    /// The 16 ANSI colors. Order is the xterm one: black, red, green, yellow,
    /// blue, magenta, cyan, white, then the eight bright variants.
    static let ansiColors: [SwiftTerm.Color] = [
        SwiftTerm.Color(red8: 0x1e, green8: 0x24, blue8: 0x30),  // black
        SwiftTerm.Color(red8: 0xe5, green8: 0x6b, blue8: 0x6f),  // red
        SwiftTerm.Color(red8: 0x5c, green8: 0xc7, blue8: 0x7f),  // green
        SwiftTerm.Color(red8: 0xe0, green8: 0xaf, blue8: 0x68),  // yellow
        SwiftTerm.Color(red8: 0x61, green8: 0xaf, blue8: 0xef),  // blue
        SwiftTerm.Color(red8: 0xc6, green8: 0x8a, blue8: 0xee),  // magenta
        SwiftTerm.Color(red8: 0x56, green8: 0xb6, blue8: 0xc2),  // cyan
        SwiftTerm.Color(red8: 0xd7, green8: 0xde, blue8: 0xe9),  // white
        SwiftTerm.Color(red8: 0x4a, green8: 0x55, blue8: 0x66),  // bright black
        SwiftTerm.Color(red8: 0xff, green8: 0x8b, blue8: 0x8f),  // bright red
        SwiftTerm.Color(red8: 0x7e, green8: 0xe7, blue8: 0x87),  // bright green
        SwiftTerm.Color(red8: 0xff, green8: 0xc9, blue8: 0x7f),  // bright yellow
        SwiftTerm.Color(red8: 0x8a, green8: 0xc8, blue8: 0xff),  // bright blue
        SwiftTerm.Color(red8: 0xdd, green8: 0xa8, blue8: 0xff),  // bright magenta
        SwiftTerm.Color(red8: 0x7f, green8: 0xd4, blue8: 0xe0),  // bright cyan
        SwiftTerm.Color(red8: 0xff, green8: 0xff, blue8: 0xff),  // bright white
    ]
}

/// Decodes the raw `wait(2)` status SwiftTerm hands to `processTerminated`.
///
/// `LocalProcess.processTerminated()` passes the value filled in by
/// `waitpid()` straight through, NOT an exit code: a child that calls
/// `_exit(127)` (the `execve` failure path in SwiftTerm's `Pty.swift`) arrives
/// here as 32512. Darwin's `W*` macros are unavailable in Swift, so decode by
/// hand: the low 7 bits are the terminating signal, the next 8 the exit code.
enum ChildStatus {
    case exited(code: Int32)
    case signalled(signal: Int32)

    init(rawWaitStatus: Int32) {
        let lowBits = rawWaitStatus & 0o177
        if lowBits == 0 {
            self = .exited(code: (rawWaitStatus >> 8) & 0xff)
        } else {
            self = .signalled(signal: lowBits)
        }
    }

    var isCleanExit: Bool {
        if case .exited(let code) = self { return code == 0 }
        return false
    }

    var description: String {
        switch self {
        case .exited(let code): return "exit status \(code)"
        case .signalled(let signal): return "killed by signal \(signal)"
        }
    }
}

/// Owns the `LocalProcessTerminalView` and the lifecycle of the Python child.
///
/// Invariants:
///  * the child dies when the app dies (``shutdown(completion:)`` — no
///    orphaned python3);
///  * the app dies when the child exits *cleanly* (``processTerminated``); a
///    failed engine leaves the window up with its diagnostics on screen.
final class TerminalHost: NSObject, LocalProcessTerminalViewDelegate {

    /// The absolute interpreter path. Deliberately NOT `/usr/bin/env python3`:
    /// this child inherits UltraBackup.app's Full Disk Access, so the
    /// interpreter must never be picked out of an inherited `PATH` where a
    /// Homebrew/pyenv/conda (or user-writable) directory could win. This is
    /// Apple's Command Line Tools stub — the single external requirement.
    static let interpreter = "/usr/bin/python3"

    /// The view to install as the window's content view.
    let terminalView: LocalProcessTerminalView

    /// Called once the child process has exited cleanly, so the app can shut
    /// down. Never called when the engine failed: the diagnostics stay up.
    var onProcessExit: (() -> Void)?

    /// pid of the child, captured at launch: `terminate()` leaves
    /// `LocalProcess.shellPid` populated even after the child was reaped, so
    /// it is never a safe liveness signal on its own.
    private var childPid: pid_t = 0

    /// Set from ``processTerminated``. SwiftTerm has already `waitpid`-ed the
    /// child by then, so the pid is free for the kernel to recycle: after this
    /// flag is set we must never signal it again.
    private var childExited = false

    private var didShutDown = false

    override init() {
        var options = TerminalOptions.default
        options.cols = TerminalTheme.columns
        options.rows = TerminalTheme.rows
        options.termName = "xterm-256color"
        options.scrollback = 5_000

        // A zero-sized frame makes SwiftTerm honor options.cols/rows verbatim
        // instead of deriving them from pixels; the window is then sized from
        // getOptimalFrameSize() below.
        terminalView = LocalProcessTerminalView(frame: .zero,
                                                font: TerminalTheme.font,
                                                options: options)
        super.init()
        terminalView.processDelegate = self
        applyTheme()
    }

    private func applyTheme() {
        terminalView.installColors(TerminalTheme.ansiColors)
        terminalView.nativeBackgroundColor = TerminalTheme.background
        terminalView.nativeForegroundColor = TerminalTheme.foreground
        terminalView.selectedTextBackgroundColor = TerminalTheme.selection
        terminalView.caretColor = TerminalTheme.caret
    }

    /// Pixel size of a `columns` x `rows` grid in the current font.
    func preferredContentSize() -> NSSize {
        let optimal = terminalView.getOptimalFrameSize().size
        guard optimal.width > 1, optimal.height > 1 else {
            return NSSize(width: 960, height: 620)
        }
        return NSSize(width: ceil(optimal.width), height: ceil(optimal.height))
    }

    // MARK: - Child process

    /// Environment for the child, built from an explicit ALLOW-LIST rather
    /// than from `ProcessInfo.processInfo.environment`.
    ///
    /// The child runs with UltraBackup.app's Full Disk Access, so nothing the
    /// user happens to export may reach it: `PYTHONHOME`, `PYTHONSTARTUP`,
    /// `PYTHONPATH`, `VIRTUAL_ENV`, `CONDA_PREFIX` can silently redirect or
    /// break the bundled package, and `DYLD_INSERT_LIBRARIES` would preload
    /// code into an FDA-privileged process.
    ///
    /// `PYTHONPATH`/cwd point at `Contents/Resources`, where build_app.sh
    /// copies the `ultrabackup` package — the app is self-contained and does
    /// not depend on the repository path.
    private func childEnvironment(payloadRoot: String) -> [String] {
        let inherited = ProcessInfo.processInfo.environment
        var env: [String: String] = [:]

        // The only inherited values the engine actually needs: HOME (every
        // discovery path is under it), USER (backup.py:45) and TMPDIR.
        for key in ["HOME", "USER", "LOGNAME", "TMPDIR", "__CF_USER_TEXT_ENCODING"] {
            if let value = inherited[key], !value.isEmpty {
                env[key] = value
            }
        }

        // Launched from Finder there is no shell profile. The engine calls
        // ditto/xattr/ioreg/sw_vers by absolute path but plutil, launchctl and
        // codesign by name, so the system tool directories — and ONLY those —
        // go on PATH.
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"

        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        env["LANG"] = "en_US.UTF-8"
        env["LC_CTYPE"] = "en_US.UTF-8"
        env["ULTRABACKUP_HOST_APP"] = "UltraBackup"
        env["PYTHONPATH"] = payloadRoot
        env["PYTHONUNBUFFERED"] = "1"
        // Never let the interpreter drop __pycache__ next to the sources: in
        // the .app those sources live inside the signed bundle, and a single
        // stray .pyc breaks the sealed resources (and therefore the FDA grant).
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        return env.map { "\($0.key)=\($0.value)" }
    }

    /// Absolute path of the directory that CONTAINS the `ultrabackup` package.
    ///
    /// Inside the .app that is `Contents/Resources`. Running the bare SwiftPM
    /// binary (`.build/release/UltraBackupApp`, the dev loop)
    /// `Bundle.main.resourcePath` is *not* nil — it is the executable's own
    /// directory — so trusting it would set `PYTHONPATH=.build/release` and
    /// the engine would die instantly with `No module named ultrabackup`.
    /// Resolve the payload explicitly instead, and verify it exists.
    static func resolvePayloadRoot() -> String? {
        let fm = FileManager.default
        var candidates: [String] = []

        if Bundle.main.bundleURL.pathExtension == "app", let resources = Bundle.main.resourcePath {
            candidates.append(resources)
        } else {
            // Outside a bundle `bundleURL` is the executable's directory.
            // Walk up looking for the repository checkout.
            var directory = Bundle.main.bundleURL.resolvingSymlinksInPath()
            for _ in 0..<6 {
                candidates.append(directory.path)
                directory.deleteLastPathComponent()
            }
        }

        return candidates.first {
            fm.fileExists(atPath: $0 + "/ultrabackup/__main__.py")
        }
    }

    func start() {
        guard let payloadRoot = TerminalHost.resolvePayloadRoot() else {
            reportStartupFailure("""
                the `ultrabackup` package was not found.

                Looked next to: \(Bundle.main.bundleURL.path)
                Expected:       <root>/ultrabackup/__main__.py

                Rebuild the bundle with native/build_app.sh, or run the binary \
                from a checkout that still contains the Python package.
                """)
            return
        }
        guard FileManager.default.isExecutableFile(atPath: TerminalHost.interpreter) else {
            reportStartupFailure("""
                \(TerminalHost.interpreter) is missing.

                Install Apple's Command Line Tools:  xcode-select --install
                """)
            return
        }

        terminalView.startProcess(executable: TerminalHost.interpreter,
                                  args: ["-m", "ultrabackup", "tui"],
                                  environment: childEnvironment(payloadRoot: payloadRoot),
                                  currentDirectory: payloadRoot)
        childPid = terminalView.process.shellPid
    }

    /// Paint a failure into the (still live) terminal view instead of starting
    /// a doomed child. Nothing calls `onProcessExit`, so the window stays up
    /// and the user can read what went wrong.
    private func reportStartupFailure(_ message: String) {
        childExited = true
        didShutDown = true
        let body = message.replacingOccurrences(of: "\n", with: "\r\n")
        terminalView.feed(text: "\r\n\u{1b}[31mUltraBackup could not start the engine: "
                                + "\u{1b}[0m\r\n\r\n\(body)\r\n\r\nPress Cmd-Q to close.\r\n")
    }

    // MARK: - Shutdown

    /// True while the engine is still alive and therefore still interruptible.
    var isEngineRunning: Bool {
        !childExited && childPid > 0 && terminalView.process.running
    }

    /// Terminate the child and make sure nothing survives this app.
    ///
    /// The child is a session leader (forkpty calls setsid), so its process
    /// group id equals its pid: signalling the group also reaches anything the
    /// engine spawned (`ditto`...). SIGINT goes first so the Python side can
    /// unwind through `KeyboardInterrupt` and restore the terminal; SIGTERM
    /// and finally SIGKILL escalate from there, so quitting can never leave an
    /// orphaned python3.
    ///
    /// - Parameter completion: when non-nil the escalation is ASYNCHRONOUS and
    ///   `completion` runs on the main queue once the child is gone. This must
    ///   be used from `applicationShouldTerminate`: SwiftTerm closes the pty
    ///   master (the SIGHUP we rely on) and delivers the child-exit event on
    ///   the MAIN queue, so blocking that queue would prevent the very
    ///   shutdown we are waiting for. Passing nil falls back to a short
    ///   bounded synchronous drain, for paths that bypass AppKit's
    ///   terminate-later handshake.
    func shutdown(completion: (() -> Void)? = nil) {
        guard !didShutDown else {
            completion?()
            return
        }
        didShutDown = true

        // The child has already been reaped by SwiftTerm (or never started):
        // its pid may already have been recycled onto an unrelated process, so
        // signalling it now would be shooting at a stranger.
        guard isEngineRunning else {
            terminalView.terminate()
            completion?()
            return
        }

        let pid = childPid
        killpg(pid, SIGINT)

        guard let completion else {
            escalateSynchronously(pid: pid)
            return
        }
        escalate(pid: pid, completion: completion)
    }

    /// True once the child is gone. `childExited` is checked FIRST: once
    /// SwiftTerm has reaped it, `kill(pid, 0)` says nothing about our child.
    private func hasExited(_ pid: pid_t) -> Bool {
        if childExited { return true }
        var status: Int32 = 0
        if waitpid(pid, &status, WNOHANG) == pid { return true }
        return kill(pid, 0) != 0 && errno == ESRCH
    }

    /// Asynchronous escalation, driven entirely from the main queue so the pty
    /// close and the process-exit event stay deliverable while we wait.
    private func escalate(pid: pid_t, completion: @escaping () -> Void) {
        // Grace window for the SIGINT already sent.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self else { completion(); return }
            if self.hasExited(pid) {
                self.terminalView.terminate()
                completion()
                return
            }
            // Closing the pty master delivers SIGHUP to the session; the same
            // call also sends SIGTERM to the leader.
            self.terminalView.terminate()
            killpg(pid, SIGTERM)
            self.poll(pid: pid, until: Date().addingTimeInterval(1.2)) { [weak self] gone in
                if !gone {
                    killpg(pid, SIGKILL)
                    kill(pid, SIGKILL)
                    _ = self?.hasExited(pid)
                }
                completion()
            }
        }
    }

    private func poll(pid: pid_t, until deadline: Date, done: @escaping (Bool) -> Void) {
        if hasExited(pid) { done(true); return }
        if Date() >= deadline { done(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
            guard let self else { done(false); return }
            self.poll(pid: pid, until: deadline, done: done)
        }
    }

    /// Fallback for callers that cannot wait asynchronously. The wait spins
    /// the run loop rather than `usleep`-ing, so SwiftTerm's main-queue pty
    /// cleanup can still run while we hold the thread.
    private func escalateSynchronously(pid: pid_t) {
        terminalView.terminate()
        killpg(pid, SIGTERM)
        if drainRunLoop(untilExitOf: pid, timeout: 0.5) { return }
        killpg(pid, SIGKILL)
        kill(pid, SIGKILL)
        _ = drainRunLoop(untilExitOf: pid, timeout: 0.2)
    }

    private func drainRunLoop(untilExitOf pid: pid_t, timeout: TimeInterval) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if hasExited(pid) { return true }
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.025))
        }
        return hasExited(pid)
    }

    // MARK: - LocalProcessTerminalViewDelegate

    func sizeChanged(source: LocalProcessTerminalView, newCols: Int, newRows: Int) {
        // The view already pushed the new winsize to the pty; nothing to do.
    }

    func setTerminalTitle(source: LocalProcessTerminalView, title: String) {
        // The window keeps its own title: the TUI's escape sequences must not
        // rename the app's window.
    }

    func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {
        // Not surfaced in the UI.
    }

    func processTerminated(source: TerminalView, exitCode: Int32?) {
        // SwiftTerm has already waitpid'd the child at this point, so the pid
        // is free to be recycled: record that and never signal it again.
        childExited = true

        // `exitCode` is the RAW wait status, not an exit code (see
        // ChildStatus). nil means SwiftTerm could not determine one at all —
        // treat that as a failure too, so the window never vanishes silently.
        guard let raw = exitCode else {
            reportEngineFailure("UltraBackup engine exited (status unavailable)")
            return
        }
        let status = ChildStatus(rawWaitStatus: raw)
        guard status.isCleanExit else {
            reportEngineFailure("UltraBackup engine exited: \(status.description)")
            return
        }

        // Clean exit — the user quit the TUI. Give the run loop a beat so the
        // engine's final output is painted before the window disappears.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.onProcessExit?()
        }
    }

    /// The engine died on its own and did not exit cleanly: keep the window,
    /// keep whatever the TUI last painted, and say what happened. Quitting the
    /// app here would flash a window for 400 ms and destroy every diagnostic
    /// (an `execve` failure, a Python traceback, the Command Line Tools
    /// installer dialog...).
    private func reportEngineFailure(_ message: String) {
        didShutDown = true
        terminalView.feed(text: "\r\n\u{1b}[31m\(message)\u{1b}[0m\r\n"
                                + "Press Cmd-Q to close.\r\n")
    }
}
