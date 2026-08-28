"""Backup execution: ditto discovered items into a timestamped backup dir.

Invariants honored here: payload copies go exclusively through
``fsutil.ditto_copy`` (xattrs/ACLs/resource forks/symlinks preserved),
symlinks are never followed (``os.lstat`` only), per-item failures never
abort the run, and ``manifest.json`` is written last and atomically so its
presence marks a complete backup.
"""

from __future__ import annotations

import copy
import getpass
import os
import shutil
import socket
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

from . import fsutil, report

if TYPE_CHECKING:
    from .discovery import AppInfo

SCHEMA_VERSION = 1
TOOL_VERSION = "2.0.0"


def _safe_call(fn, default=""):
    """Run a cosmetic probe (ioreg/sw_vers); never let it abort a backup."""
    try:
        return fn()
    except Exception:
        return default


def _username() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USER", "")


def _json_safe(value):
    """Deep-convert Path objects so the manifest is JSON-serializable."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(val) for val in value]
    return value


def _create_backup_dir(dest_root: Path, app_name: str, now: datetime) -> Path:
    """Create ``<dest>/<Name>_<YYYY-MM-DDTHH-MM-SS>/`` with mode 700."""
    timestamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    candidate = dest_root / "{}_{}".format(app_name, timestamp)
    attempt = 1
    while True:
        try:
            candidate.mkdir()
            break
        except FileExistsError:
            # Same-second collision: uniquify rather than merge into an
            # existing backup directory.
            attempt += 1
            candidate = dest_root / "{}_{}-{}".format(app_name, timestamp, attempt)
    os.chmod(candidate, 0o700)
    return candidate


def _type_from_stat(st: os.stat_result) -> str:
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return "file"


def _discard_partial(dst: Path) -> None:
    """Best-effort removal of a payload tree left behind by a failed ditto."""
    try:
        st = os.lstat(dst)
    except OSError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(dst, ignore_errors=True)
    else:
        try:
            os.unlink(dst)
        except OSError:
            pass


def _copy_item(item: dict, index: int, payload_root: Path, home: Path,
               warnings: List[str]) -> dict:
    """Copy one discovered item into the payload; never raises.

    Returns the manifest ``items`` entry with status
    ``copied`` | ``permission_denied`` | ``missing``.
    """
    src = fsutil.from_portable(str(item["path"]), home)
    item_id = str(item.get("id") or "{:04d}".format(index))
    entry = {
        "id": item_id,
        "category": item.get("category", ""),
        "original_path": fsutil.to_portable(src, home),
        "type": item.get("type", "file"),
        "ownership": item.get("ownership", "user"),
        "mode": "0000",
        "size_bytes": 0,
        "provenance": item.get("provenance", "template"),
        "status": "copied",
        "files": [],
    }

    discovered_status = item.get("status", "found")
    if discovered_status == "missing":
        entry["status"] = "missing"
        return entry
    if discovered_status == "permission_denied":
        entry["status"] = "permission_denied"
        warnings.append("Cannot read {}: permission denied.".format(entry["original_path"]))
        return entry

    # Root-owned items are still READ-backupable when world-readable (the
    # common case for /Applications bundles); an unreadable one degrades to
    # permission_denied via the lstat/ditto failure paths below. The euid
    # gate matters only at restore time (writing/chown), never for reading.
    try:
        st = os.lstat(src)
    except FileNotFoundError:
        entry["status"] = "missing"
        return entry
    except PermissionError:
        entry["status"] = "permission_denied"
        warnings.append("Cannot read {}: permission denied.".format(entry["original_path"]))
        return entry
    except OSError as exc:
        entry["status"] = "permission_denied"
        warnings.append("Cannot stat {}: {}".format(entry["original_path"], exc))
        return entry

    entry["type"] = _type_from_stat(st)
    entry["mode"] = format(stat.S_IMODE(st.st_mode), "04o")

    dst = payload_root / item_id / src.name

    # SPEC invariant 1: never follow symlinks. `ditto` DEREFERENCES a
    # top-level symlink source and copies the pointed-to tree (a link into
    # the home would swallow arbitrary home content), so an item that is
    # itself a symlink is recorded and recreated as a link — never dittoed.
    if entry["type"] == "symlink":
        try:
            link_target = os.readlink(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link_target, dst)
        except OSError as exc:
            _discard_partial(dst)
            entry["status"] = "permission_denied"
            warnings.append(
                "could not record symlink {}: {}".format(entry["original_path"], exc)
            )
            return entry
        entry["files"] = [{"relpath": ".", "type": "symlink", "target": link_target}]
        entry["status"] = "copied"
        return entry

    try:
        fsutil.ditto_copy(src, dst)
    except (PermissionError, subprocess.CalledProcessError, OSError) as exc:
        _discard_partial(dst)
        if not os.path.lexists(src):
            entry["status"] = "missing"
        else:
            entry["status"] = "permission_denied"
            warnings.append(
                "ditto failed for {}: {}".format(entry["original_path"], exc)
            )
        return entry

    entry["files"] = fsutil.hash_tree(dst)
    entry["size_bytes"] = sum(
        f.get("size") or 0 for f in entry["files"] if isinstance(f, dict)
    )
    entry["status"] = "copied"
    return entry


def do_backup(app: "AppInfo", items: List[dict], dest_root: Path,
              home: Path = None,
              progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Back up *items* of *app* into ``<dest_root>/<Name>_<ts>/``.

    Copies each found item with ditto into ``payload/<id>/<basename>``,
    hashing each copy to fill the item's ``files`` list. Per-item failures
    become status ``permission_denied``/``missing`` without aborting the
    run; any ``permission_denied`` makes completeness PARTIAL. The manifest
    is written atomically as the final step.

    ``progress``, when given, is called once per completed item with a copy
    of that item's manifest entry (id, category, status, size_bytes, ...).
    It is purely informational (used by the TUI for live logging): exceptions
    it raises are swallowed so a faulty callback can never abort a backup.

    Returns ``{"backup_dir": Path, "manifest": dict, "partial": bool}``.
    """
    home = Path(home) if home is not None else Path.home()
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now().astimezone()
    backup_dir = _create_backup_dir(dest_root, app.name, now)
    payload_root = backup_dir / "payload"
    payload_root.mkdir()

    warnings: List[str] = []
    entries: List[dict] = []
    for index, item in enumerate(items, start=1):
        entry = _copy_item(item, index, payload_root, home, warnings)
        entries.append(entry)
        if progress is not None:
            try:
                # Deep copy: the entry (incl. its nested "files" list) is
                # later serialized into manifest.json, so a mutating callback
                # must never be able to corrupt the recorded hashes.
                progress(copy.deepcopy(entry))
            except Exception:  # noqa: BLE001 - callback must never abort a backup
                pass

    partial = any(e["status"] == "permission_denied" for e in entries)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": now.isoformat(timespec="seconds"),
        "tool_version": TOOL_VERSION,
        "source": {
            "username": _username(),
            "hostname": _safe_call(socket.gethostname),
            "hardware_uuid": _safe_call(fsutil.hardware_uuid),
            "macos_version": _safe_call(fsutil.macos_version),
            "home": str(home),
        },
        "app": {
            "name": app.name,
            "bundle_id": app.bundle_id,
            "version": getattr(app, "version", None),
            "path": str(app.path) if app.path is not None else None,
            "mas_receipt": bool(getattr(app, "mas_receipt", False)),
            "helpers": _json_safe(list(getattr(app, "helpers", None) or [])),
        },
        "completeness": "PARTIAL" if partial else "COMPLETE",
        "not_capturable": list(report.NOT_CAPTURABLE),
        "items": entries,
    }

    # Written last: a payload without manifest.json is an invalid backup.
    fsutil.atomic_write_json(backup_dir / "manifest.json", manifest)

    for warning in warnings:
        print("warning: {}".format(warning), file=sys.stderr)

    return {"backup_dir": backup_dir, "manifest": manifest, "partial": partial}
