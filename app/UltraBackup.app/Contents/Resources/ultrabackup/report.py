"""Exit codes and human-readable reporting for UltraBackup.

Holds the process exit-code contract, the canonical list of things macOS
forbids a file-level backup from capturing, and the printers used by the
CLI for capability reports and restore plans.
"""

from __future__ import annotations

from typing import Iterable, Optional

(
    EXIT_OK,
    EXIT_ERROR,
    EXIT_USAGE,
    EXIT_PARTIAL,
    EXIT_VERIFY_MISMATCH,
    EXIT_ROLLED_BACK,
    EXIT_ROLLBACK_INCOMPLETE,
    EXIT_NEEDS_CONFIRMATION,
) = 0, 1, 2, 3, 4, 5, 6, 7

# What macOS forbids or breaks for a file-level per-app backup. Printed with
# every capability report and at the end of each run.
NOT_CAPTURABLE = [
    "Keychain items and Electron safeStorage secrets: the app will ask you to "
    "log in again; Chromium cookies/tokens from another machine are undecipherable",
    "TCC privacy grants (Full Disk Access, screen recording, microphone, ...): "
    "re-grant in System Settings > Privacy & Security",
    "BTM background/login items: re-approve in System Settings > General > "
    "Login Items",
    "Mac App Store receipt (_MASReceipt): cross-machine, reinstall the app "
    "from the App Store",
    "iCloud-synced data: managed by CloudKit; warned about and skipped",
    "SIP-protected system files",
    "Hardlinks and sparse files: ditto materializes them as full regular copies",
]


def print_capability_report(subject) -> None:
    """Print what was (or will be) captured vs. what macOS makes impossible.

    ``subject`` is either a backup manifest dict (its ``items`` are listed
    with their status) or an app object/dict (``name`` / ``bundle_id``), in
    which case only the identity header and the limitations are printed.
    """
    items = None
    if isinstance(subject, dict) and "items" in subject:
        app = subject.get("app") or {}
        name = app.get("name", "?")
        bundle_id = app.get("bundle_id", "?")
        items = subject["items"]
        not_capturable = subject.get("not_capturable") or NOT_CAPTURABLE
    else:
        name = _field(subject, "name", "?")
        bundle_id = _field(subject, "bundle_id", "?")
        not_capturable = NOT_CAPTURABLE

    print(f"Capability report — {name} ({bundle_id})")
    if items is not None:
        print()
        print("Captured:")
        rows = [
            (
                item.get("category", "?"),
                str(item.get("original_path", "?")),
                item.get("status", "?"),
            )
            for item in items
        ]
        _print_table(("CATEGORY", "PATH", "STATUS"), rows, indent="  ")
        copied = sum(1 for item in items if item.get("status") == "copied")
        print(f"  {copied}/{len(items)} items captured")
    print()
    print("NOT capturable (macOS restrictions):")
    for line in not_capturable:
        print(f"  - {line}")


def print_plan(plan: Iterable) -> None:
    """Print a restore plan as an aligned table.

    Columns: action, category, target, conflict, live_newer, plus the skip
    reason when there is one.
    """
    entries = list(plan)
    rows = []
    for entry in entries:
        item = entry.get("item") or {}
        rows.append(
            (
                entry.get("action", "?"),
                item.get("category", "?"),
                str(entry.get("target", "?")),
                "yes" if entry.get("conflict") else "no",
                "yes" if entry.get("live_newer") else "no",
                entry.get("reason") or "-",
            )
        )
    _print_table(
        ("ACTION", "CATEGORY", "TARGET", "CONFLICT", "LIVE_NEWER", "REASON"), rows
    )
    restores = sum(1 for entry in entries if entry.get("action") == "restore")
    skips = len(entries) - restores
    print()
    print(f"{restores} to restore, {skips} skipped")


def _print_table(headers: tuple, rows: list, indent: str = "") -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    lines = [headers] + rows
    for line in lines:
        rendered = "  ".join(
            str(cell).ljust(widths[index]) for index, cell in enumerate(line)
        )
        print(f"{indent}{rendered.rstrip()}")


def _field(subject, name: str, default: Optional[str] = None):
    if isinstance(subject, dict):
        return subject.get(name, default)
    return getattr(subject, name, default)
