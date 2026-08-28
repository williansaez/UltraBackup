//
//  AppDelegate.swift
//  UltraBackupApp
//
//  Window, main menu and lifecycle. No storyboard, no nib: everything is
//  built here so the bundle stays a plain SwiftPM executable.
//

import AppKit
import Darwin

final class AppDelegate: NSObject, NSApplicationDelegate {

    private var window: NSWindow?
    private var host: TerminalHost?
    private var sigtermSource: DispatchSourceSignal?

    /// Where we are in the terminate-later handshake.
    private enum QuitState {
        /// No quit requested yet.
        case idle
        /// The "engine still running" sheet is up, waiting for the user.
        case confirming
        /// The child is being torn down; `NSApp.reply` is still owed.
        case shuttingDown
    }

    private var quitState: QuitState = .idle

    /// True when the quit did NOT come from the user (SIGTERM/logout/`pkill`,
    /// or the engine having exited by itself). Those must never be blocked by
    /// a confirmation sheet.
    private var quitIsInvoluntary = false

    private var confirmationAlert: NSAlert?

    /// `reply(toApplicationShouldTerminate:)` is owed exactly once.
    private var didReplyToTerminate = false

    // MARK: - Terminate plumbing

    /// Ask AppKit to quit — never by calling `NSApp.terminate` inline.
    ///
    /// `NSApp.terminate` spins a NESTED event loop while it waits for
    /// `reply(toApplicationShouldTerminate:)` (verified with `sample`:
    /// `-[NSApplication _shouldTerminate]` -> `nextEventMatchingMask:`).
    /// libdispatch will not re-enter the main queue while a main-queue block
    /// is still on the stack, so calling terminate from inside one — the
    /// SIGTERM DispatchSource handler, or the `processTerminated` follow-up —
    /// means our own reply can never be delivered and the app hangs forever
    /// with the window up. Handing the call to the RUN LOOP instead keeps the
    /// main queue drainable underneath the nested loop.
    private func requestTerminate() {
        RunLoop.main.perform(inModes: [.common]) {
            NSApp.terminate(nil)
        }
    }

    /// Idempotent: the reply may be raced by the shutdown watchdog.
    private func replyToTerminate(_ shouldTerminate: Bool) {
        guard !didReplyToTerminate else { return }
        didReplyToTerminate = true
        NSApp.reply(toApplicationShouldTerminate: shouldTerminate)
    }

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.mainMenu = AppDelegate.makeMainMenu()

        let host = TerminalHost()
        self.host = host
        host.onProcessExit = { [weak self] in
            // The engine is already gone: there is nothing left to confirm.
            self?.quitIsInvoluntary = true
            self?.requestTerminate()
        }

        let contentSize = host.preferredContentSize()
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: contentSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "UltraBackup"
        // Dark titlebar + a window background that matches the terminal, so
        // resizing never flashes a light strip behind the grid.
        window.appearance = NSAppearance(named: .darkAqua)
        window.backgroundColor = TerminalTheme.background
        window.isReleasedWhenClosed = false
        // The window is rebuilt from scratch on every launch and has no
        // identifier, so AppKit would write and replay restoration state that
        // can never be used.
        window.isRestorable = false
        window.setContentSize(contentSize)
        window.contentMinSize = NSSize(width: 480, height: 320)

        let terminalView = host.terminalView
        terminalView.frame = NSRect(origin: .zero, size: contentSize)
        terminalView.autoresizingMask = [.width, .height]
        window.contentView = terminalView

        window.center()
        window.makeKeyAndOrderFront(nil)
        window.makeFirstResponder(terminalView)
        self.window = window

        NSApp.activate(ignoringOtherApps: true)
        host.start()

        // Strictly AFTER the fork: SIG_IGN is inherited across fork/exec, and
        // a child that ignores SIGTERM could not be shut down politely.
        installSignalHandling()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    /// Opt in to secure coding for restorable state (macOS 12+ logs a warning
    /// on every launch otherwise).
    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool {
        return true
    }

    /// Cmd-Q / the close button must not kill a backup mid-copy without asking:
    /// the engine's process group includes the in-flight `ditto`, and neither
    /// the TUI nor `backup.py` can roll back a tree it never finished writing.
    ///
    /// Everything returns `.terminateLater` so the teardown runs
    /// asynchronously — a synchronous wait here would block the main queue,
    /// which is exactly the queue SwiftTerm uses to close the pty master and
    /// to deliver the child's exit.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        switch quitState {
        case .shuttingDown:
            return .terminateLater
        case .confirming:
            // A second quit request while the sheet is up (SIGTERM, logout):
            // stop asking and go.
            if quitIsInvoluntary {
                dismissConfirmation()
                beginShutdown()
            }
            return .terminateLater
        case .idle:
            guard let host, host.isEngineRunning, !quitIsInvoluntary, let window else {
                beginShutdown()
                return .terminateLater
            }
            presentConfirmation(in: window)
            return .terminateLater
        }
    }

    private func presentConfirmation(in window: NSWindow) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Quit UltraBackup?"
        alert.informativeText = """
            The backup engine is still running. Quitting now interrupts \
            whatever it is copying, and a partially written backup has no \
            completion marker.

            Return to the window and leave the TUI with “q” to stop cleanly.
            """
        alert.addButton(withTitle: "Keep Running")
        alert.addButton(withTitle: "Quit Anyway")

        quitState = .confirming
        confirmationAlert = alert
        // The red close button orders the window out BEFORE AppKit asks us to
        // terminate; a sheet on a hidden window would be invisible and the
        // user would just see the app refuse to quit. Bring it back first.
        if !window.isVisible {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
        // A sheet, not runModal: a modal loop would stall the main queue and
        // the SIGTERM source with it.
        alert.beginSheetModal(for: window) { [weak self] response in
            guard let self, self.quitState == .confirming else { return }
            self.confirmationAlert = nil
            if response == .alertSecondButtonReturn {
                self.beginShutdown()
            } else {
                self.quitState = .idle
                self.replyToTerminate(false)
                // Another quit may follow; a fresh reply will be owed then.
                self.didReplyToTerminate = false
            }
        }
    }

    private func dismissConfirmation() {
        guard let alert = confirmationAlert else { return }
        confirmationAlert = nil
        window?.endSheet(alert.window, returnCode: .alertSecondButtonReturn)
    }

    private func beginShutdown() {
        guard quitState != .shuttingDown else { return }
        quitState = .shuttingDown

        // Safety net: the engine teardown must never strand the app in
        // AppKit's terminate-later wait with its window on screen. Scheduled
        // off the main queue and delivered through the run loop, so it stands
        // even if the main queue is momentarily blocked.
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 5) {
            RunLoop.main.perform(inModes: [.common]) { [weak self] in
                self?.replyToTerminate(true)
            }
        }

        // Always async: `reply(toApplicationShouldTerminate:)` may only be
        // sent AFTER applicationShouldTerminate has returned .terminateLater,
        // and TerminalHost.shutdown can complete synchronously when the child
        // is already gone.
        DispatchQueue.main.async { [weak self] in
            guard let self else {
                NSApp.reply(toApplicationShouldTerminate: true)
                return
            }
            guard let host = self.host else {
                self.replyToTerminate(true)
                return
            }
            host.shutdown { [weak self] in
                self?.replyToTerminate(true)
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        // Belt and braces for any path that reaches termination without going
        // through applicationShouldTerminate. A no-op once shutdown ran.
        host?.shutdown()
    }

    /// Route SIGTERM (`pkill UltraBackup`, a logout, a `killall`) through the
    /// normal AppKit shutdown so the child-process cleanup always runs.
    /// Without this the default disposition kills the app outright and the
    /// engine is only reaped by the pty's SIGHUP, which is a weaker guarantee.
    private func installSignalHandling() {
        signal(SIGTERM, SIG_IGN)
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        source.setEventHandler { [weak self] in
            guard let self else {
                NSApp.terminate(nil)
                return
            }
            // Not the user's choice: never hold a logout behind a sheet.
            self.quitIsInvoluntary = true
            switch self.quitState {
            case .shuttingDown:
                break  // already on its way out
            case .confirming:
                // AppKit is still waiting on the outstanding .terminateLater,
                // so answer that instead of asking to terminate again.
                self.dismissConfirmation()
                self.beginShutdown()
            case .idle:
                self.requestTerminate()
            }
        }
        source.resume()
        sigtermSource = source
    }

    // MARK: - Main menu

    private static func makeMainMenu() -> NSMenu {
        let mainMenu = NSMenu()

        // App menu. Its title is supplied by AppKit from CFBundleName.
        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenuItem.submenu = appMenu

        appMenu.addItem(withTitle: "About UltraBackup",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide UltraBackup",
                        action: #selector(NSApplication.hide(_:)),
                        keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others",
                                         action: #selector(NSApplication.hideOtherApplications(_:)),
                                         keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Show All",
                        action: #selector(NSApplication.unhideAllApplications(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit UltraBackup",
                        action: #selector(NSApplication.terminate(_:)),
                        keyEquivalent: "q")

        // Edit menu — without it cmd-C / cmd-V do nothing in the terminal
        // view, because those are menu key equivalents, not key events the
        // view would ever see.
        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenuItem.submenu = editMenu
        editMenu.addItem(withTitle: "Copy",
                         action: #selector(NSText.copy(_:)),
                         keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste",
                         action: #selector(NSText.paste(_:)),
                         keyEquivalent: "v")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSResponder.selectAll(_:)),
                         keyEquivalent: "a")

        // Window menu.
        let windowMenuItem = NSMenuItem()
        mainMenu.addItem(windowMenuItem)
        let windowMenu = NSMenu(title: "Window")
        windowMenuItem.submenu = windowMenu
        windowMenu.addItem(withTitle: "Minimize",
                           action: #selector(NSWindow.performMiniaturize(_:)),
                           keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Zoom",
                           action: #selector(NSWindow.performZoom(_:)),
                           keyEquivalent: "")
        NSApp.windowsMenu = windowMenu

        return mainMenu
    }
}
