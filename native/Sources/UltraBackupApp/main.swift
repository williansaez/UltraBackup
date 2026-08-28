//
//  main.swift
//  UltraBackupApp
//
//  Entry point. NSApplication is wired up by hand — no storyboard, no
//  @NSApplicationMain — so the executable stays a plain SwiftPM product that
//  build_app.sh drops into UltraBackup.app/Contents/MacOS/.
//

import AppKit

let application = NSApplication.shared

// .regular: a real app with a Dock icon and a menu bar. TCC (Full Disk
// Access) is attributed to this bundle, and the Python engine inherits it as
// our child process.
application.setActivationPolicy(.regular)

let appDelegate = AppDelegate()
application.delegate = appDelegate

application.run()
