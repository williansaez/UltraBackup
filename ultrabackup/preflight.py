"""Preflight checks: running app processes, Full Disk Access, destination health.

Every check is read-only except the destination fidelity probe
(``fsutil.dest_fidelity_check``), which creates and removes a temporary
file inside the destination directory.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from . import fsutil

if TYPE_CHECKING:
    from .discovery import AppInfo

# Canary path for the Full Disk Access probe: the Safari container is
# present on every stock macOS install and unreadable without FDA.
_FDA_CANARY = ("Library", "Containers", "com.apple.Safari")

# Deep link to the exact System Settings pane where FDA is granted.
FDA_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
)

_TERM_PROGRAM_NAMES = {
    "Apple_Terminal": "Terminal",
    "iTerm.app": "iTerm2",
    "vscode": "Visual Studio Code",
    "WarpTerminal": "Warp",
    "ghostty": "Ghostty",
    "Hyper": "Hyper",
    "Tabby": "Tabby",
}


def terminal_host_app() -> str:
    """Human name of the app hosting this TTY — the one that needs the FDA
    grant, since TCC attaches to the host process, not to python3.

    ``ULTRABACKUP_HOST_APP`` wins over ``TERM_PROGRAM``: the native
    UltraBackup.app sets it when it spawns this interpreter inside its own
    embedded terminal, and there the grant belongs to UltraBackup itself —
    ``TERM_PROGRAM`` would be absent or (worse) inherited from whatever
    terminal happened to launch the app.
    """
    host_app = os.environ.get("ULTRABACKUP_HOST_APP", "").strip()
    if host_app:
        return host_app
    term_program = os.environ.get("TERM_PROGRAM", "")
    return _TERM_PROGRAM_NAMES.get(term_program, term_program or "seu app de terminal")


def open_fda_settings() -> bool:
    """Open System Settings at the Full Disk Access pane. Read-only for the
    system: granting still requires the user's click there."""
    try:
        subprocess.run(["/usr/bin/open", FDA_SETTINGS_URL], check=True,
                       capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def _load_known_apps() -> dict:
    """Load known_apps.json from the package root (or the repo root)."""
    pkg_dir = Path(__file__).resolve().parent
    for candidate in (pkg_dir / "known_apps.json", pkg_dir.parent / "known_apps.json"):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return {}


def _extras_dirs(app: "AppInfo", home: Path) -> List[Path]:
    """Curated extras directories for *app* (e.g. ``~/.claude``).

    Only entries that exist and are real directories (lstat, symlinks not
    followed) are returned — a process cannot be running "from" a file.
    """
    dirs: List[Path] = []
    for entry in _load_known_apps().values():
        names = entry.get("match_names", [])
        bundle_ids = entry.get("match_bundle_ids", [])
        matched = getattr(app, "name", None) in names or (
            getattr(app, "bundle_id", None) and app.bundle_id in bundle_ids
        )
        if not matched:
            continue
        for extra in entry.get("extras", []):
            path = fsutil.from_portable(extra, home)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                dirs.append(path)
    return dirs


def running_processes(app: "AppInfo", home: Path = None) -> List[str]:
    """Return descriptions of live processes that belong to *app*.

    Matches the app bundle path and each curated extras directory as a
    LITERAL substring of every process's command line (``ps -axo
    pid=,command=``). ``pgrep -f`` is deliberately not used: it interprets
    the path as an extended regex, so bundle names containing ``()``/``[]``
    etc. silently never match (defeating the running-app corruption guard)
    and the tool's own argv (which contains the bundle path) self-matches.
    This process and its parent are excluded for the same reason.
    An empty list means the app is not running.
    """
    home = Path(home) if home is not None else Path.home()
    patterns: List[str] = []
    if getattr(app, "path", None):
        patterns.append(str(app.path))
    for extra_dir in _extras_dirs(app, home):
        patterns.append(str(extra_dir))
    if not patterns:
        return []

    proc = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return []

    own_pids = {str(os.getpid()), str(os.getppid())}
    seen_pids = set()
    descriptions: List[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, cmd = line.partition(" ")
        pid, cmd = pid.strip(), cmd.strip()
        if pid in own_pids or pid in seen_pids:
            continue
        if any(pattern in cmd for pattern in patterns):
            seen_pids.add(pid)
            descriptions.append("pid {}: {}".format(pid, cmd or "?"))
    return descriptions


def fda_probe(home: Path = None) -> str:
    """Probe Full Disk Access: ``"ok"``, ``"denied"`` or ``"unknown"``.

    Attempts to list the Safari container under *home*. TCC blocks the
    read without FDA (PermissionError -> denied); a missing canary path
    (fake test homes, unusual installs) is inconclusive -> unknown.
    """
    home = Path(home) if home is not None else Path.home()
    canary = home.joinpath(*_FDA_CANARY)
    try:
        os.listdir(canary)
    except PermissionError:
        return "denied"
    except FileNotFoundError:
        return "unknown"
    except OSError:
        return "unknown"
    return "ok"


def _nearest_existing_dir(path: Path) -> Optional[Path]:
    """Closest existing directory at or above *path* (for statvfs/W_OK)."""
    current = Path(os.path.abspath(os.path.expanduser(str(path))))
    while True:
        if os.path.isdir(current):
            return current
        if current.parent == current:
            return None
        current = current.parent


def _cloud_sync_location(dest: Path) -> Optional[str]:
    """Name of the cloud-sync service *dest* lives under, if any."""
    parts = Path(os.path.abspath(os.path.expanduser(str(dest)))).parts
    for i, part in enumerate(parts):
        if part == "Mobile Documents":
            return "iCloud Drive"
        if part == "CloudStorage":
            provider = parts[i + 1] if i + 1 < len(parts) else ""
            return provider or "a cloud-synced folder"
        if part.startswith("Dropbox"):
            return "Dropbox"
        if part.startswith("OneDrive"):
            return "OneDrive"
    return None


def doctor(app: "AppInfo | None", dest: Path, home: Path = None,
           need_bytes: int = 0) -> dict:
    """Aggregate preflight checks for a backup/restore run.

    Returns ``{"ok": bool, "problems": [...], "warnings": [...]}``.
    ``ok`` is False iff there is at least one problem. Checks: app running,
    Full Disk Access, free space vs *need_bytes*, destination writability
    and copy fidelity (xattr/symlink support), effective uid when
    root-owned items are present, and a warning when the destination is
    inside a cloud-synced folder (iCloud Drive/Dropbox/OneDrive).
    """
    home = Path(home) if home is not None else Path.home()
    dest = Path(dest)
    problems: List[str] = []
    warnings: List[str] = []

    if app is not None:
        procs = running_processes(app, home=home)
        if procs:
            problems.append(
                "App appears to be running ({}). Quit it before backup/restore; "
                "--force proceeds at risk of corrupting live databases.".format(
                    "; ".join(procs)
                )
            )

    fda = fda_probe(home=home)
    if fda == "denied":
        problems.append(
            "Full Disk Access is denied for '{host}'. Enable it: System "
            "Settings > Privacy & Security > Full Disk Access > turn ON "
            "'{host}' (run `open \"{url}\"` to jump there), then QUIT and "
            "reopen {host}. Without it, containers/cookies/HTTPStorages "
            "will be skipped.".format(host=terminal_host_app(),
                                      url=FDA_SETTINGS_URL)
        )
    elif fda == "unknown":
        warnings.append(
            "Could not determine Full Disk Access status (canary path absent)."
        )

    probe_dir = _nearest_existing_dir(dest)
    if probe_dir is None:
        problems.append("Destination {} has no existing ancestor directory.".format(dest))
    else:
        if os.path.exists(dest) and not os.path.isdir(dest):
            problems.append(
                "Destination {} exists and is not a directory.".format(dest)
            )
        writable_dir = dest if os.path.isdir(dest) else probe_dir
        if not os.access(writable_dir, os.W_OK | os.X_OK):
            problems.append("Destination {} is not writable.".format(writable_dir))
        else:
            try:
                free = fsutil.free_space(probe_dir)
                if need_bytes and free < need_bytes:
                    problems.append(
                        "Not enough free space at {}: need {} bytes, "
                        "{} available.".format(writable_dir, need_bytes, free)
                    )
            except OSError as exc:
                warnings.append(
                    "Could not determine free space at {}: {}".format(probe_dir, exc)
                )
            try:
                problems.extend(fsutil.dest_fidelity_check(writable_dir))
            except Exception as exc:  # probe failure itself is inconclusive
                warnings.append(
                    "Destination fidelity check failed to run: {}".format(exc)
                )

    if app is not None:
        try:
            from . import discovery
            items = discovery.discover(app, home=home)
        except Exception:
            items = []
        root_items = [
            i for i in items
            if i.get("ownership") == "root" and i.get("status") != "missing"
        ]
        if root_items and os.geteuid() != 0:
            warnings.append(
                "{} root-owned item(s) present; they are backed up if "
                "readable, but RESTORING them will require sudo.".format(
                    len(root_items)
                )
            )

    cloud = _cloud_sync_location(dest)
    if cloud is not None:
        warnings.append(
            "Destination is inside {} — the backup (which contains secrets "
            "such as OAuth tokens and cookies) will be synced to the cloud, "
            "and sync churn can slow or alter it.".format(cloud)
        )

    return {"ok": not problems, "problems": problems, "warnings": warnings}
