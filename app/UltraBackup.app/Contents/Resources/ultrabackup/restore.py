"""Restore engine for UltraBackup.

Implements the restore contract from SPEC.md: loading and validating a backup,
planning a restore (dry-run friendly), applying it with a crash-safe journal,
rolling back, verifying payload integrity, and detecting version skew.

Hard invariants honored here:
- Symlinks are never followed (``os.lstat`` / ``os.path.lexists`` only).
- All payload copies go through ``ditto`` (via ``fsutil.ditto_copy``).
- ``ditto`` merges into an existing directory, so the target is asserted
  absent before every ditto call (data-corruption guard).
- ``subprocess`` is always invoked with argument lists, never ``shell=True``.
- The restore journal is written atomically BEFORE each mutation.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

try:
    import plistlib
except ImportError:  # pragma: no cover - plistlib is stdlib on all targets
    plistlib = None

from .fsutil import (
    atomic_write_json,
    ditto_copy,
    from_portable,
    hardware_uuid,
    hash_tree,
    macos_version,
    run,
    sha256_file,
)

# Tests flip this off to avoid mutating the live system: when False, the
# `defaults` / `launchctl` / `codesign` / `lsregister` (and quarantine xattr)
# helpers are not invoked and preferences fall back to a raw ditto copy.
system_calls_enabled = True

SUPPORTED_SCHEMA_VERSION = 1
JOURNAL_NAME = "restore-journal.json"
PRERESTORE_DIRNAME = ".ultrabackup-prerestore"
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
CONTAINER_METADATA_PLIST = ".com.apple.containermanagerd.metadata.plist"

_SPECIAL_BY_CATEGORY = {
    "preferences": "preferences",
    "containers": "container",
    "launch_agents": "launch_agent",
}

_UUID_RE = re.compile(r"^(?P<domain>.+)\.(?P<uuid>[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12})$")


# ---------------------------------------------------------------------------
# Loading / planning
# ---------------------------------------------------------------------------

def load_backup(backup_dir: Path) -> dict:
    """Load and validate ``manifest.json`` from a backup directory.

    Raises ``FileNotFoundError`` when the manifest is absent (a backup
    without a manifest is incomplete/invalid by definition — the manifest is
    written last, atomically) and ``ValueError`` on schema problems.
    """
    backup_dir = Path(backup_dir)
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"{backup_dir} is not a valid UltraBackup: manifest.json is missing "
            "(the backup is incomplete or was interrupted)"
        )
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest.json in {backup_dir} is not valid JSON: {exc}") from exc

    version = manifest.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported manifest schema_version {version!r} in {backup_dir} "
            f"(this tool supports version {SUPPORTED_SCHEMA_VERSION})"
        )
    if not isinstance(manifest.get("items"), list):
        raise ValueError(f"manifest.json in {backup_dir} has no 'items' list")
    return manifest


def plan_restore(
    manifest: dict,
    backup_dir: Path,
    home: Path = None,
    only: Optional[list] = None,
    exclude: Optional[list] = None,
) -> list:
    """Build the restore plan (no mutations).

    Returns a list of action dicts::

        {"item": <manifest item>, "target": Path,
         "action": "restore" | "skip", "reason": str | None,
         "conflict": bool, "live_newer": bool,
         "special": None | "preferences" | "container" | "launch_agent"}

    ``conflict`` means the target currently exists (checked with lexists, so
    a dangling symlink counts). ``live_newer`` means the live target's lstat
    mtime is newer than the backup's ``created_at`` — apply_restore skips
    those unless ``overwrite_newer`` is passed.
    """
    home = Path.home() if home is None else Path(home)
    backup_dir = Path(backup_dir)
    only_set = {c.strip() for c in only} if only else None
    exclude_set = {c.strip() for c in exclude} if exclude else set()
    created_epoch = _iso_to_epoch(manifest.get("created_at", ""))

    plan = []
    for item in manifest.get("items", []):
        category = item.get("category", "")
        target = from_portable(item.get("original_path", ""), home)
        special = _SPECIAL_BY_CATEGORY.get(category)

        entry = {
            "item": item,
            "target": target,
            "action": "restore",
            "reason": None,
            "conflict": False,
            "live_newer": False,
            "special": special,
        }

        # Containment/ownership validation against a corrupt or malicious
        # manifest (path traversal, ownership spoofing, unsafe payload ids).
        # Enforced regardless of --force, and re-checked in apply_entry.
        unsafe = _unsafe_item_reason(item, target, home)
        if unsafe:
            entry["action"] = "skip"
            entry["reason"] = "unsafe manifest entry: {}".format(unsafe)
            plan.append(entry)
            continue

        if only_set is not None and category not in only_set:
            entry["action"] = "skip"
            entry["reason"] = "category not selected (--only)"
        elif category in exclude_set:
            entry["action"] = "skip"
            entry["reason"] = "category excluded (--exclude)"
        elif item.get("status") != "copied":
            entry["action"] = "skip"
            entry["reason"] = f"not captured in backup (status: {item.get('status')})"
        elif not os.path.lexists(_payload_root(backup_dir, item)):
            entry["action"] = "skip"
            entry["reason"] = "payload missing from backup"
        elif os.geteuid() != 0:
            if item.get("ownership") == "root":
                entry["action"] = "skip"
                entry["reason"] = ("root-owned item (re-run with sudo to "
                                   "restore it)")
            elif not _is_within(target, home) and not _parent_writable(target):
                entry["action"] = "skip"
                entry["reason"] = ("no write permission on the target's "
                                   "parent (re-run with sudo)")

        if os.path.lexists(target):
            entry["conflict"] = True
            if created_epoch is not None:
                try:
                    if os.lstat(target).st_mtime > created_epoch:
                        entry["live_newer"] = True
                except OSError:
                    pass

        plan.append(entry)
    return plan


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_restore(
    plan: list,
    backup_dir: Path,
    home: Path = None,
    overwrite_newer: bool = False,
    strip_quarantine: bool = False,
) -> dict:
    """Apply a restore plan, following SPEC.md's numbered steps.

    The journal is written atomically before every mutation, existing targets
    are moved aside to a same-volume ``.ultrabackup-prerestore`` directory,
    and any exception triggers an automatic rollback from the journal.

    Returns a dict with keys: ``ok``, ``restored``, ``skipped``, ``warnings``,
    ``moved_aside``, ``rolled_back``, ``rollback_complete`` (None when no
    rollback happened, else bool), ``journal`` and optionally ``error``.
    """
    home = Path.home() if home is None else Path(home)
    backup_dir = Path(backup_dir)
    manifest = load_backup(backup_dir)
    session = _RestoreSession(backup_dir, home, manifest)

    try:
        for entry in plan:
            session.apply_entry(entry, overwrite_newer=overwrite_newer,
                                strip_quarantine=strip_quarantine)
    except Exception as exc:  # noqa: BLE001 - any failure must roll back
        complete, problems = _undo_journal(session.journal)
        session.warnings.extend(problems)
        if complete:
            # Guard a later `rollback` run from re-undoing recovered state.
            session.journal["rolled_back"] = True
            session.journal["rolled_back_at"] = datetime.now().astimezone().isoformat()
            try:
                session.write_journal()
            except OSError as journal_exc:
                session.warnings.append(
                    f"could not mark journal rolled back: {journal_exc}"
                )
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "restored": session.restored,
            "skipped": session.skipped,
            "warnings": session.warnings,
            "moved_aside": session.journal["moved_aside"],
            "rolled_back": True,
            "rollback_complete": complete,
            "journal": str(session.journal_path),
        }

    return {
        "ok": True,
        "restored": session.restored,
        "skipped": session.skipped,
        "warnings": session.warnings,
        "moved_aside": session.journal["moved_aside"],
        "rolled_back": False,
        "rollback_complete": None,
        "journal": str(session.journal_path),
    }


class _RestoreSession:
    """Mutable state for one apply_restore run (journal, results, warnings)."""

    def __init__(self, backup_dir: Path, home: Path, manifest: dict):
        self.backup_dir = backup_dir
        self.home = home
        self.manifest = manifest
        self.timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        self.journal_path = backup_dir / JOURNAL_NAME
        self.journal = {
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "created_at": datetime.now().astimezone().isoformat(),
            "backup_dir": str(backup_dir),
            "moved_aside": [],
            "restored": [],
            "created_dirs": [],
            "rolled_back": False,
        }
        self.restored = []
        self.skipped = []
        self.warnings = []

    # -- bookkeeping -------------------------------------------------------

    def write_journal(self) -> None:
        atomic_write_json(self.journal_path, self.journal)

    def skip(self, entry: dict, reason: str, warn: bool = False) -> None:
        self.skipped.append({"target": str(entry["target"]), "reason": reason,
                             "category": entry["item"].get("category")})
        if warn:
            self.warnings.append(f"skipped {entry['target']}: {reason}")

    def record_restore(self, item_id: str, path: Path, method: str = "ditto") -> None:
        """Journal an imminent restore mutation (written before the mutation)."""
        self.journal["restored"].append(
            {"item_id": item_id, "path": str(path), "method": method}
        )
        self.write_journal()

    def ensure_dirs(self, directory: Path) -> None:
        """``mkdir -p`` that journals every directory it actually creates.

        Rollback removes journaled created directories with ``rmdir`` (never
        recursive), so a restore into a location whose parents did not exist
        before the restore is fully undone instead of leaving empty trees.
        """
        directory = Path(directory)
        missing = []
        current = directory
        while not os.path.lexists(current) and current != current.parent:
            missing.append(current)
            current = current.parent
        if not missing:
            return
        # Journal BEFORE the mutation (spec: journal precedes every mutation).
        for path in reversed(missing):
            self.journal["created_dirs"].append(str(path))
        self.write_journal()
        for path in reversed(missing):
            os.mkdir(path)

    # -- move-aside (spec step 2) -----------------------------------------

    def move_aside(self, target: Path, item_id: str) -> Path:
        """Move an existing target out of the way, same-volume, journaled.

        Uses ``os.rename``; on EXDEV falls back to ditto copy + verify, and
        deletes the source only after the copy verified identical.
        """
        base = _prerestore_base(target, self.home)
        prerestore_root = base / PRERESTORE_DIRNAME
        aside_dir = prerestore_root / self.timestamp / item_id
        aside_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(prerestore_root, 0o700)
        except OSError:
            pass
        aside = aside_dir / target.name

        self.journal["moved_aside"].append(
            {"item_id": item_id, "original": str(target), "aside": str(aside)}
        )
        self.write_journal()

        try:
            os.rename(target, aside)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            _assert_absent(aside)
            ditto_copy(target, aside)
            mismatches = _compare_trees(target, aside)
            if mismatches:
                raise RuntimeError(
                    f"cross-volume move-aside of {target} did not verify: {mismatches[:3]}"
                )
            _remove_path(target)
        return aside

    # -- per-entry dispatch ------------------------------------------------

    def apply_entry(self, entry: dict, overwrite_newer: bool,
                    strip_quarantine: bool) -> None:
        item = entry["item"]
        target = Path(entry["target"])

        if entry.get("action") != "restore":
            self.skip(entry, entry.get("reason") or "skipped by plan")
            return
        # Containment/ownership re-validation: a hand-crafted plan must not
        # bypass the plan_restore check. Enforced regardless of --force.
        unsafe = _unsafe_item_reason(item, target, self.home)
        if unsafe:
            self.skip(entry, "unsafe manifest entry: {}".format(unsafe), warn=True)
            return
        # Spec step 9: live target newer than the backup and no override.
        if entry.get("live_newer") and not overwrite_newer:
            self.skip(entry, "live target is newer than backup "
                             "(pass --overwrite-newer to replace it)", warn=True)
            return
        # Items outside the home need root (never sudo automatically).
        if item.get("ownership") == "root" and os.geteuid() != 0:
            self.skip(entry, "requires root (re-run with sudo to restore "
                             "items outside the home)", warn=True)
            return

        payload = _payload_root(self.backup_dir, item)
        if not os.path.lexists(payload):
            self.skip(entry, "payload missing from backup", warn=True)
            return

        special = entry.get("special") or _SPECIAL_BY_CATEGORY.get(item.get("category"))
        restored = True
        if special == "preferences":
            restored = self.restore_preferences(item, target, payload)
        elif special == "container":
            self.restore_container(item, target, payload)
        elif special == "launch_agent":
            self.restore_launch_agent(item, target, payload)
        else:
            self.restore_standard(item, target, payload)
            if item.get("category") == "app_bundle":
                self.postprocess_app_bundle(target, strip_quarantine)
        if restored is False:
            # Nothing was mutated (e.g. corrupt plist rejected before any
            # move-aside): this is a skip, never a "restored" item.
            self.skip(entry, "backup payload failed validation "
                             "(plutil -lint); item not restored")
            return
        self.restored.append({"target": str(target),
                              "category": item.get("category"),
                              "id": item.get("id")})

    # -- restore flavors ---------------------------------------------------

    def restore_standard(self, item: dict, target: Path, payload: Path) -> None:
        """Spec step 3: move-aside, assert absent, ditto, chmod."""
        item_id = item.get("id", "?")
        if os.path.lexists(target):
            self.move_aside(target, item_id)
        self.record_restore(item_id, target)
        _assert_absent(target)
        self.ensure_dirs(target.parent)
        if item.get("type") == "symlink":
            link_target = _symlink_target(item, payload)
            os.symlink(link_target, target)
        else:
            ditto_copy(payload, target)
            _apply_mode(target, item.get("mode"))
        # Home items keep the current user's ownership: no chown (spec inv. 9).

    def restore_preferences(self, item: dict, target: Path, payload: Path) -> bool:
        """Spec step 4: plutil -lint + defaults import; ByHost via -currentHost.

        ``plutil -lint`` validates the backup payload BEFORE anything is
        mutated: a corrupt plist must never displace the live preferences
        file. Returns False (and mutates nothing) when lint rejects the
        payload; the caller records the item as skipped, not restored.

        Falls back to a raw ditto copy (with a warning) when `defaults import`
        fails or when system calls are disabled for testing.
        """
        item_id = item.get("id", "?")
        byhost = "ByHost" in target.parts
        source_uuid = (self.manifest.get("source") or {}).get("hardware_uuid", "")
        domain = _plist_domain(target.name, byhost, source_uuid)

        # Spec step 4 gate: lint FIRST, before any move-aside or import.
        if system_calls_enabled:
            lint = run(["plutil", "-lint", str(payload)], check=False)
            if lint.returncode != 0:
                self.warnings.append(
                    f"plutil -lint failed for {payload}; preferences item "
                    f"{domain} not restored (live plist left untouched)"
                )
                return False

        if byhost and system_calls_enabled:
            # Re-key the plist name to THIS machine's hardware UUID.
            try:
                current_uuid = hardware_uuid()
                target = target.parent / f"{domain}.{current_uuid}.plist"
            except Exception as exc:  # noqa: BLE001 - degrade to source name
                self.warnings.append(
                    f"could not read hardware UUID ({exc}); keeping source "
                    f"ByHost name for {target.name}"
                )

        if os.path.lexists(target):
            self.move_aside(target, item_id)

        imported = False
        if system_calls_enabled:
            if byhost:
                cmd = ["defaults", "-currentHost", "import", domain, str(payload)]
            else:
                cmd = ["defaults", "import", domain, str(payload)]
            self.record_restore(item_id, target, method="defaults-import")
            result = run(cmd, check=False)
            imported = result.returncode == 0
            if not imported:
                self.warnings.append(
                    f"defaults import failed for domain {domain}; falling back "
                    "to raw plist copy"
                )
        else:
            self.record_restore(item_id, target, method="raw-copy")
            self.warnings.append(
                f"system calls disabled: preferences {domain} restored via raw copy"
            )

        if not imported:
            _assert_absent(target)
            self.ensure_dirs(target.parent)
            ditto_copy(payload, target)
            _apply_mode(target, item.get("mode"))
        return True

    def restore_container(self, item: dict, target: Path, payload: Path) -> None:
        """Spec step 5: restore ONLY the contents of Data/.

        Excludes the containermanagerd metadata plist and top-level symlinks
        in Data/ that redirect into the home (~/Documents, ~/Desktop, ...) —
        restoring those would clobber the real directories.
        """
        item_id = item.get("id", "?")
        data_src = payload / "Data"
        if not os.path.isdir(data_src) or os.path.islink(data_src):
            self.warnings.append(
                f"container payload for {item.get('original_path')} has no "
                "Data/ directory; nothing restored"
            )
            return

        source_home = (self.manifest.get("source") or {}).get("home", "")
        target_data = target / "Data"
        # Journaled mkdir: when the container did not exist before the
        # restore, rollback must remove these directories again.
        self.ensure_dirs(target_data)

        # Links pointing inside the container itself are internal, not redirects.
        container_roots = [str(target)]
        home_bases = [str(self.home)]
        if source_home:
            home_bases.append(source_home)
            container_roots.append(
                str(from_portable(item.get("original_path", ""), Path(source_home)))
            )

        for name in sorted(os.listdir(data_src)):
            if name == CONTAINER_METADATA_PLIST:
                continue
            src_child = data_src / name
            if os.path.islink(src_child) and _is_home_redirect(
                src_child, target_data, home_bases, container_roots
            ):
                continue
            dst_child = target_data / name
            if os.path.lexists(dst_child):
                self.move_aside(dst_child, item_id)
            self.record_restore(item_id, dst_child)
            _assert_absent(dst_child)
            if os.path.islink(src_child):
                os.symlink(os.readlink(src_child), dst_child)
            else:
                ditto_copy(src_child, dst_child)

    def restore_launch_agent(self, item: dict, target: Path, payload: Path) -> None:
        """Spec step 6: bootout (ignoring errors), copy, then bootstrap."""
        item_id = item.get("id", "?")
        in_home = _is_within(target, self.home)
        label = target.name[:-len(".plist")] if target.name.endswith(".plist") else target.name
        uid = os.getuid()

        if in_home and system_calls_enabled:
            run(["launchctl", "bootout", f"gui/{uid}/{label}"], check=False)

        if os.path.lexists(target):
            self.move_aside(target, item_id)
        self.record_restore(item_id, target)
        _assert_absent(target)
        self.ensure_dirs(target.parent)
        ditto_copy(payload, target)
        _apply_mode(target, item.get("mode"))

        if in_home and system_calls_enabled:
            result = run(["launchctl", "bootstrap", f"gui/{uid}", str(target)],
                         check=False)
            if result.returncode != 0:
                self.warnings.append(
                    f"launchctl bootstrap failed for {label} (agent copied; "
                    "it will load at next login)"
                )

    def postprocess_app_bundle(self, target: Path, strip_quarantine: bool) -> None:
        """Spec step 7: quarantine handling, codesign verify, lsregister."""
        if not system_calls_enabled:
            return
        probe = run(["xattr", "-p", "com.apple.quarantine", str(target)], check=False)
        if probe.returncode == 0:
            if strip_quarantine:
                run(["xattr", "-dr", "com.apple.quarantine", str(target)], check=False)
            else:
                self.warnings.append(
                    f"{target} carries com.apple.quarantine; Gatekeeper may "
                    "block it (use --strip-quarantine to remove)"
                )
        result = run(["codesign", "--verify", "--deep", "--strict", str(target)],
                     check=False)
        if result.returncode != 0:
            self.warnings.append(
                f"codesign verification failed for {target}; the app may not "
                "launch (signature was damaged before backup or in transit)"
            )
        run([LSREGISTER, "-f", str(target)], check=False)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback(backup_dir: Path, home: Path = None) -> dict:
    """Undo the last ``apply_restore`` using its journal.

    Removes restored targets, then moves every moved-aside original back
    (reverse order). Returns ``{"ok", "complete", "problems", "undone"}``;
    ``complete`` is False when any step could not be undone (CLI exit 6).
    """
    backup_dir = Path(backup_dir)
    journal_path = backup_dir / JOURNAL_NAME
    if not journal_path.is_file():
        return {"ok": False, "complete": False, "undone": 0,
                "problems": [f"no {JOURNAL_NAME} in {backup_dir}: nothing to roll back"]}
    with open(journal_path, "r", encoding="utf-8") as fh:
        journal = json.load(fh)
    if journal.get("rolled_back"):
        return {"ok": True, "complete": True, "undone": 0,
                "problems": [], "note": "journal already rolled back"}

    complete, problems = _undo_journal(journal)
    undone = len(journal.get("restored", [])) + len(journal.get("moved_aside", []))
    if complete:
        journal["rolled_back"] = True
        journal["rolled_back_at"] = datetime.now().astimezone().isoformat()
        atomic_write_json(journal_path, journal)
    return {"ok": complete, "complete": complete, "undone": undone,
            "problems": problems}


def _undo_journal(journal: dict) -> "tuple[bool, list]":
    """Best-effort undo of a journal; returns (complete, problems)."""
    problems = []
    for rec in reversed(journal.get("restored", [])):
        path = Path(rec["path"])
        try:
            if os.path.lexists(path):
                _remove_path(path)
        except OSError as exc:
            problems.append(f"rollback: could not remove restored {path}: {exc}")
    for rec in reversed(journal.get("moved_aside", [])):
        original = Path(rec["original"])
        aside = Path(rec["aside"])
        try:
            if not os.path.lexists(aside):
                continue  # move-aside was journaled but never executed
            if os.path.lexists(original):
                problems.append(
                    f"rollback: {original} still occupied; aside copy kept at {aside}"
                )
                continue
            _move_back(aside, original)
        except (OSError, RuntimeError) as exc:
            problems.append(f"rollback: could not move {aside} back to {original}: {exc}")
    # Directories the restore created (journaled by ensure_dirs) are removed
    # deepest-first with rmdir — never recursive, so directories that gained
    # unrelated content in the meantime are deliberately left alone.
    for path in reversed(journal.get("created_dirs", [])):
        try:
            os.rmdir(path)
        except OSError:
            pass
    return (not problems), problems


def _move_back(aside: Path, original: Path) -> None:
    original.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(aside, original)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _assert_absent(original)
        ditto_copy(aside, original)
        mismatches = _compare_trees(aside, original)
        if mismatches:
            raise RuntimeError(f"cross-volume move-back did not verify: {mismatches[:3]}")
        _remove_path(aside)


# ---------------------------------------------------------------------------
# Verify / version skew
# ---------------------------------------------------------------------------

def verify(backup_dir: Path) -> dict:
    """Re-hash every copied payload tree against the manifest.

    Returns ``{"ok": bool, "mismatches": [{"item_id", "relpath", "reason"}]}``.
    """
    backup_dir = Path(backup_dir)
    manifest = load_backup(backup_dir)
    mismatches = []

    for item in manifest.get("items", []):
        if item.get("status") != "copied":
            continue
        item_id = item.get("id", "?")
        try:
            payload = _payload_root(backup_dir, item)
        except ValueError as exc:
            mismatches.append({"item_id": item_id, "relpath": ".",
                               "reason": f"unsafe manifest entry: {exc}"})
            continue
        if not os.path.lexists(payload):
            mismatches.append({"item_id": item_id, "relpath": ".",
                               "reason": "payload missing"})
            continue
        expected = _files_index(item.get("files", []))
        actual = _files_index(_compute_files(payload))
        for relpath in sorted(set(expected) | set(actual)):
            exp, act = expected.get(relpath), actual.get(relpath)
            if exp is None:
                mismatches.append({"item_id": item_id, "relpath": relpath,
                                   "reason": "extra file in payload"})
            elif act is None:
                mismatches.append({"item_id": item_id, "relpath": relpath,
                                   "reason": "file missing from payload"})
            else:
                reason = _entry_mismatch(exp, act)
                if reason:
                    mismatches.append({"item_id": item_id, "relpath": relpath,
                                       "reason": reason})
    return {"ok": not mismatches, "mismatches": mismatches}


def version_skew_check(manifest: dict, home: Path = None) -> list:
    """Compare the installed app / machine with the manifest; return warnings.

    The CLI blocks the restore on non-empty warnings unless ``--force``.
    """
    home = Path.home() if home is None else Path(home)
    warnings = []
    app = manifest.get("app") or {}
    source = manifest.get("source") or {}

    app_path = app.get("path")
    if app_path:
        bundle = Path(app_path)
        # A missing app is NOT version skew: there is no installed version to
        # diverge from (restore-after-reinstall is the primary use case), so
        # it must not block the restore. The CLI prints its own info notice.
        if bundle.exists():
            installed = _installed_version(bundle)
            backed_up = app.get("version")
            if installed and backed_up and installed != backed_up:
                warnings.append(
                    f"installed app version {installed} differs from backup "
                    f"version {backed_up}; data formats may be incompatible"
                )

    if source.get("home") and str(home) != source["home"]:
        warnings.append(
            f"backup was made under home {source['home']}; paths will be "
            f"remapped to {home} (cross-machine degraded mode)"
        )

    if system_calls_enabled:
        try:
            if source.get("hardware_uuid") and hardware_uuid() != source["hardware_uuid"]:
                warnings.append(
                    "backup was created on a different machine: Keychain, "
                    "safeStorage-encrypted data and TCC grants will not carry over"
                )
        except Exception:  # noqa: BLE001 - informational check only
            pass
        try:
            if source.get("macos_version") and macos_version() != source["macos_version"]:
                warnings.append(
                    f"macOS version differs (backup: {source['macos_version']}, "
                    f"current: {macos_version()})"
                )
        except Exception:  # noqa: BLE001 - informational check only
            pass
    return warnings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_safe_component(name: str) -> bool:
    """True when *name* is usable as a single path component (no traversal)."""
    return bool(name) and name not in (".", "..") and "/" not in name \
        and os.sep not in name and "\x00" not in name


def _payload_root(backup_dir: Path, item: dict) -> Path:
    """Payload location for *item*; raises ValueError on unsafe id/basename.

    The manifest ``id`` and the basename of ``original_path`` are attacker-
    controlled in a corrupt manifest; both must stay single path components
    so the ditto SOURCE can never escape ``<backup>/payload/``.
    """
    item_id = str(item.get("id", ""))
    basename = Path(str(item.get("original_path", ""))).name
    if not _is_safe_component(item_id):
        raise ValueError(f"manifest item id {item_id!r} is not a safe path component")
    if not _is_safe_component(basename):
        raise ValueError(
            f"manifest original_path basename {basename!r} is not a safe path component"
        )
    return Path(backup_dir) / "payload" / item_id / basename


# Roots an outside-the-home item may restore into. All discovery categories
# that produce out-of-home items live under these (/Applications,
# /Library/{Application Support,LaunchAgents,LaunchDaemons}).
_ROOT_RESTORE_ROOTS = (Path("/Applications"), Path("/Library"))


def _parent_writable(target: Path) -> bool:
    """Whether the nearest existing ancestor of *target*'s parent allows
    creating/renaming entries (move-aside and ditto both need it)."""
    cur = Path(os.path.abspath(str(target))).parent
    while not os.path.lexists(cur) and cur != cur.parent:
        cur = cur.parent
    return os.access(cur, os.W_OK | os.X_OK)


def _unsafe_item_reason(item: dict, target: Path, home: Path) -> Optional[str]:
    """Reason a manifest item must never be restored, or None when safe.

    Guards against a corrupt/malicious manifest (SPEC threat: path traversal
    on restore): rejects ``..`` components and unsafe payload ids, and
    requires the resolved target to sit inside the allowed roots (the home,
    /Applications, or /Library). Ownership is validated only for shape —
    since it reflects the real owner uid, not the location, it carries no
    containment information (a spoofed 'user' on a root-owned target fails
    later at the OS write and is rolled back). Never bypassed by --force.
    """
    portable = str(item.get("original_path", ""))
    if not portable:
        return "missing original_path"
    if ".." in Path(portable).parts:
        return f"original_path {portable!r} contains a '..' component"
    item_id = str(item.get("id", ""))
    if not _is_safe_component(item_id):
        return f"item id {item_id!r} is not a safe path component"
    if not _is_safe_component(Path(portable).name):
        return f"original_path {portable!r} basename is not a safe path component"

    target = Path(target)
    if Path(os.path.realpath(target)) == Path(os.path.realpath(home)):
        return "target resolves to the home directory itself"
    ownership = item.get("ownership", "user")
    if ownership not in ("user", "root"):
        return f"unknown ownership {ownership!r}"
    if not _is_within(target, home) and not any(
        _is_within(target, root) for root in _ROOT_RESTORE_ROOTS
    ):
        return (f"target {target} resolves outside the allowed roots "
                f"(the home, /Applications, /Library)")
    return None


def _assert_absent(path: Path) -> None:
    """Guard against ditto's merge-into-existing-directory behavior."""
    if os.path.lexists(path):
        raise RuntimeError(
            f"refusing to ditto onto existing path {path} (ditto merges into "
            "existing directories; target must be moved aside first)"
        )


def _apply_mode(target: Path, mode: Optional[str]) -> None:
    if not mode or os.path.islink(target):
        return
    try:
        os.chmod(target, int(mode, 8))
    except (OSError, ValueError):
        pass


def _symlink_target(item: dict, payload: Path) -> str:
    files = item.get("files") or []
    for entry in files:
        if entry.get("relpath") == "." and entry.get("type") == "symlink":
            return entry.get("target", "")
    if os.path.islink(payload):
        return os.readlink(payload)
    raise RuntimeError(
        f"manifest item {item.get('id')} is a symlink but records no target"
    )


def _is_within(path: Path, root: Path) -> bool:
    """Containment check on RESOLVED paths, never a lexical one.

    The parent of *path* is realpath-resolved (symlinks and any ``..``
    reaching through existing components collapse), so a traversal target
    like ``~/../../etc/x`` is judged by where it actually lands, not by its
    spelling. The final component is kept unresolved: the write happens at
    ``parent/name`` even when ``name`` is currently a symlink.
    """
    path = Path(path)
    root_real = Path(os.path.realpath(root))
    resolved = Path(os.path.realpath(path.parent)) / path.name
    try:
        resolved.relative_to(root_real)
        return True
    except ValueError:
        return False


def _nearest_existing(path: Path) -> Path:
    current = Path(path)
    while not os.path.lexists(current) and current != current.parent:
        current = current.parent
    return current


def _prerestore_base(target: Path, home: Path) -> Path:
    """Directory on the SAME device as target that hosts the move-aside area.

    ``os.path.ismount`` walking is wrong on macOS: ``/Library`` lives on the
    Data volume via a firmlink but is not a mount point, so the old walk
    reached ``/`` (sealed, read-only, and across the firmlink boundary —
    EXDEV/EROFS). Instead the device is taken from the nearest existing
    ancestor of *target* via ``st_dev``, and the base is the topmost
    ancestor still on that device (e.g. ``/Library`` itself, or the home).
    """
    home = Path(home)
    if _is_within(target, home):
        return home
    anchor = _nearest_existing(Path(target).parent)
    try:
        device = os.lstat(anchor).st_dev
    except OSError:
        return anchor
    base = anchor
    while base != base.parent:
        parent = base.parent
        try:
            if os.lstat(parent).st_dev != device:
                break
        except OSError:
            break
        base = parent
    return base


def _is_home_redirect(link: Path, link_dir: Path, home_bases: list,
                      container_roots: list) -> bool:
    """True when a top-of-Data symlink redirects into the home proper.

    Container ``Data/`` dirs hold symlinks like ``Documents -> ~/Documents``;
    restoring them (or worse, their contents) would clobber the real home
    directories. Links that stay inside the container itself are internal
    structure and are NOT redirects.
    """
    try:
        raw = os.readlink(link)
    except OSError:
        return False
    if os.path.isabs(raw):
        resolved = os.path.normpath(raw)
    else:
        resolved = os.path.normpath(os.path.join(str(link_dir), raw))

    def _inside(path: str, base: str) -> bool:
        base = base.rstrip("/")
        return bool(base) and (path == base or path.startswith(base + os.sep))

    if any(_inside(resolved, root) for root in container_roots):
        return False
    return any(_inside(resolved, base) for base in home_bases)


def _plist_domain(basename: str, byhost: bool, source_uuid: str) -> str:
    stem = basename[:-len(".plist")] if basename.endswith(".plist") else basename
    if byhost:
        if source_uuid and stem.endswith("." + source_uuid):
            return stem[: -(len(source_uuid) + 1)]
        match = _UUID_RE.match(stem)
        if match:
            return match.group("domain")
    return stem


def _remove_path(path: Path) -> None:
    """Delete a file/dir/symlink without ever following symlinks."""
    if os.path.islink(path) or not os.path.isdir(path):
        os.remove(path)
    else:
        shutil.rmtree(path)


def _compute_files(root: Path) -> list:
    """Manifest-shaped 'files' entries for any payload root (file/dir/symlink)."""
    if os.path.islink(root):
        return [{"relpath": ".", "type": "symlink", "target": os.readlink(root)}]
    if os.path.isdir(root):
        return hash_tree(root)
    st = os.lstat(root)
    return [{"relpath": ".", "type": "file", "size": st.st_size,
             "sha256": sha256_file(root)}]


def _files_index(entries: Iterable) -> dict:
    return {entry.get("relpath"): entry for entry in entries}


def _entry_mismatch(expected: dict, actual: dict) -> Optional[str]:
    if expected.get("type") != actual.get("type"):
        return f"type changed ({expected.get('type')} -> {actual.get('type')})"
    if expected.get("type") == "symlink":
        if expected.get("target") != actual.get("target"):
            return "symlink target changed"
        return None
    exp_sha, act_sha = expected.get("sha256"), actual.get("sha256")
    if exp_sha and act_sha and exp_sha != act_sha:
        return "sha256 mismatch"
    return None


def _compare_trees(a: Path, b: Path) -> list:
    """Content comparison used to verify EXDEV ditto copies before deleting."""
    index_a = _files_index(_compute_files(a))
    index_b = _files_index(_compute_files(b))
    problems = []
    for relpath in sorted(set(index_a) | set(index_b)):
        ea, eb = index_a.get(relpath), index_b.get(relpath)
        if ea is None or eb is None:
            problems.append(f"{relpath}: present on one side only")
        else:
            reason = _entry_mismatch(ea, eb)
            if reason:
                problems.append(f"{relpath}: {reason}")
    return problems


def _installed_version(bundle: Path) -> Optional[str]:
    info = bundle / "Contents" / "Info.plist"
    try:
        with open(info, "rb") as fh:
            data = plistlib.load(fh)
        return data.get("CFBundleShortVersionString") or data.get("CFBundleVersion")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def _iso_to_epoch(timestamp: str) -> Optional[float]:
    if not timestamp:
        return None
    text = timestamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
