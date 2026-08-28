#!/bin/bash
#
# build_app.sh — builds the native UltraBackup app and assembles
# app/UltraBackup.app around it.
#
# The bundle is self-contained: the Swift binary plus a copy of the Python
# package in Contents/Resources/ultrabackup. The only external requirement is
# the system python3 (Command Line Tools). Nothing points back at this
# repository, so the .app can be moved to /Applications.
#
# Every command is called with an explicit argument list; no eval, no shell
# interpolation of user data into a command string.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname -- "$SCRIPT_DIR")"
NATIVE_DIR="$SCRIPT_DIR"
PY_PACKAGE="$REPO_ROOT/ultrabackup"
APP_BUNDLE="$REPO_ROOT/app/UltraBackup.app"
CONTENTS="$APP_BUNDLE/Contents"
MACOS_DIR="$CONTENTS/MacOS"
RESOURCES_DIR="$CONTENTS/Resources"
APP_VERSION="2.0.0"

if [ ! -d "$PY_PACKAGE" ]; then
    echo "error: python package not found at $PY_PACKAGE" >&2
    exit 1
fi

# ---------------------------------------------------------------- build ----

# --disable-automatic-resolution: Package.resolved is authoritative. Without
# it a stale lock silently resolves forward and the terminal emulator hosting
# an FDA-privileged engine changes version behind our back.
echo "==> swift build -c release"
swift build -c release --disable-automatic-resolution --package-path "$NATIVE_DIR"

BIN_PATH="$(swift build -c release --disable-automatic-resolution --package-path "$NATIVE_DIR" --show-bin-path)"
BUILT_BINARY="$BIN_PATH/UltraBackupApp"

if [ ! -x "$BUILT_BINARY" ]; then
    echo "error: built executable not found at $BUILT_BINARY" >&2
    exit 1
fi

# ------------------------------------------------------------- assemble ----

echo "==> assembling $APP_BUNDLE"
mkdir -p "$MACOS_DIR" "$RESOURCES_DIR"

# The old launcher was a bash script that asked Terminal.app (via osascript)
# to run the TUI — which is exactly why Full Disk Access had to be granted to
# Terminal. Remove it; the native binary takes its place.
rm -f "$MACOS_DIR/UltraBackup"
cp "$BUILT_BINARY" "$MACOS_DIR/UltraBackup"
chmod 755 "$MACOS_DIR/UltraBackup"

# The signature from a previous build no longer matches the new binary.
rm -rf "$CONTENTS/_CodeSignature"

if [ ! -f "$RESOURCES_DIR/AppIcon.icns" ]; then
    echo "warning: $RESOURCES_DIR/AppIcon.icns is missing; the app will use the generic icon" >&2
fi

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleName</key>
	<string>UltraBackup</string>
	<key>CFBundleDisplayName</key>
	<string>UltraBackup</string>
	<key>CFBundleIdentifier</key>
	<string>dev.ultrabackup.app</string>
	<key>CFBundleExecutable</key>
	<string>UltraBackup</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>__APP_VERSION__</string>
	<key>CFBundleVersion</key>
	<string>__APP_VERSION__</string>
	<key>LSMinimumSystemVersion</key>
	<string>13.0</string>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.utilities</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSSupportsAutomaticTermination</key>
	<false/>
	<key>NSSupportsSuddenTermination</key>
	<false/>
	<key>NSSupportsSecureRestorableState</key>
	<true/>
</dict>
</plist>
PLIST

# Substitute the version without eval: rewrite the file in place with sed.
sed -i '' -e "s/__APP_VERSION__/${APP_VERSION}/g" "$CONTENTS/Info.plist"

plutil -lint "$CONTENTS/Info.plist"

# ------------------------------------------------ python engine payload ----

echo "==> copying python package into Contents/Resources/ultrabackup"
# --delete-excluded is load bearing: plain --delete PROTECTS excluded paths on
# the receiver, so a __pycache__ that once landed inside the bundle would
# survive every rebuild forever — and a stray .pyc breaks the sealed
# resources (and with them the FDA grant).
rsync -a --delete --delete-excluded \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.DS_Store' \
    "$PY_PACKAGE/" "$RESOURCES_DIR/ultrabackup/"

if [ ! -f "$RESOURCES_DIR/ultrabackup/known_apps.json" ]; then
    echo "error: known_apps.json did not make it into the bundle" >&2
    exit 1
fi

STRAY_BYTECODE="$(find "$RESOURCES_DIR/ultrabackup" \( -name '__pycache__' -o -name '*.pyc' \) -print)"
if [ -n "$STRAY_BYTECODE" ]; then
    echo "error: python bytecode inside the signed bundle:" >&2
    echo "$STRAY_BYTECODE" >&2
    exit 1
fi

# SwiftPM resource bundle for SwiftTerm (Metal shaders). Bundle.module looks
# it up next to the main bundle's resources.
SWIFTTERM_BUNDLE="$BIN_PATH/SwiftTerm_SwiftTerm.bundle"
if [ -d "$SWIFTTERM_BUNDLE" ]; then
    rm -rf "$RESOURCES_DIR/SwiftTerm_SwiftTerm.bundle"
    # --noextattr/--norsrc: the build directory carries a com.apple.FinderInfo
    # xattr that codesign rejects outright ("resource fork, Finder
    # information, or similar detritus not allowed").
    ditto --noextattr --norsrc \
        "$SWIFTTERM_BUNDLE" "$RESOURCES_DIR/SwiftTerm_SwiftTerm.bundle"
    # SwiftPM emits the shader read-only; without u+w the xattr strip below
    # fails with EACCES (and its errors are deliberately silenced).
    chmod -R u+w "$RESOURCES_DIR/SwiftTerm_SwiftTerm.bundle"
fi

# --------------------------------------------------------------- sign ------

# Belt and braces: any FinderInfo/ResourceFork that crept in (Finder visiting
# the folder, an old copy) would make codesign refuse the bundle. These fail
# harmlessly on files that never had the attribute.
xattr -rd com.apple.FinderInfo "$APP_BUNDLE" 2>/dev/null || true
xattr -rd com.apple.ResourceFork "$APP_BUNDLE" 2>/dev/null || true

echo "==> codesign (ad-hoc)"
codesign --force --deep -s - "$APP_BUNDLE"
codesign -dv "$APP_BUNDLE" 2>&1 | sed -e 's/^/    /'

# `codesign -dv` only DISPLAYS the signature — it checks nothing. Verify the
# seal for real, or a bundle whose sealed resources no longer match ships
# looking fine (and macOS then treats it as modified and drops the FDA grant).
echo "==> codesign --verify"
codesign --verify --deep --verbose=2 "$APP_BUNDLE" 2>&1 | sed -e 's/^/    /'

# --strict additionally rejects any com.apple.FinderInfo/ResourceFork on the
# bundle. That attribute is re-attached out of our control when the checkout
# lives in a Finder- or FileProvider-managed folder (iCloud Desktop &
# Documents), so run the strict pass on a scratch copy made with
# --noextattr/--norsrc: what survives that is a genuine seal problem.
STRICT_STAGE="$(mktemp -d "${TMPDIR:-/tmp}/ultrabackup-verify.XXXXXX")"
trap 'rm -rf "$STRICT_STAGE"' EXIT
ditto --noextattr --norsrc "$APP_BUNDLE" "$STRICT_STAGE/UltraBackup.app"
codesign --verify --deep --strict --verbose=2 "$STRICT_STAGE/UltraBackup.app" 2>&1 \
    | sed -e 's/^/    /'

# Refresh Launch Services so Finder picks up the new bundle immediately.
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
if [ -x "$LSREGISTER" ]; then
    "$LSREGISTER" -f "$APP_BUNDLE" || true
fi

cat <<'NOTE'

==> Done: app/UltraBackup.app

    Run it with:  open app/UltraBackup.app

    Install it with (never cp -R: that drags Finder xattrs along):
        ditto --noextattr --norsrc app/UltraBackup.app /Applications/UltraBackup.app

    IMPORTANT — Full Disk Access
    For an ad-hoc signed app macOS keys the TCC grant to the bundle's cdhash
    AND to its path. You must RE-GRANT Full Disk Access whenever either
    changes — that is after every REBUILD *and* after MOVING or COPYING the
    bundle (for example into /Applications):
        System Settings > Privacy & Security > Full Disk Access
    (toggle UltraBackup off and on again, or remove it with "-" and re-add
    the bundle you are actually going to launch), then quit and reopen it.

    Easiest path: install into /Applications FIRST, grant FDA there once, and
    always launch that copy.

    Grant FDA to UltraBackup.app itself — Terminal no longer needs it.
NOTE
