"""Filesystem and subprocess utilities for UltraBackup.

Hard invariants enforced here:

* Symlinks are never followed: all traversal uses ``os.walk(followlinks=False)``
  and ``os.lstat``.
* Subprocesses are always invoked with argument lists, never ``shell=True``.
* Payload copies go through ``ditto`` (xattrs, ACLs, resource forks, symlinks,
  code signatures preserved).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import uuid as _uuid
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

# Fixed macOS system binaries (SIP-protected locations); absolute paths avoid
# any dependency on the caller's PATH.
_DITTO = "/usr/bin/ditto"
_XATTR = "/usr/bin/xattr"
_IOREG = "/usr/sbin/ioreg"
_SW_VERS = "/usr/bin/sw_vers"

_HASH_CHUNK_BYTES = 1024 * 1024


def run(cmd: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """Run *cmd* as an argument list (never a shell) and return the result.

    Args:
        cmd: Command and arguments as a list of strings.
        check: Raise ``subprocess.CalledProcessError`` on non-zero exit.
        capture: Capture stdout/stderr as text on the returned object.
    """
    if not isinstance(cmd, (list, tuple)):
        raise TypeError("run() requires an argument list, never a shell string")
    return subprocess.run(
        [str(c) for c in cmd],
        check=check,
        capture_output=capture,
        text=True,
    )


def ditto_copy(src: Path, dst: Path) -> None:
    """Copy *src* to *dst* with ``ditto``, preserving macOS metadata.

    Creates parent directories of *dst*. Raises ``FileExistsError`` if *dst*
    already exists: ditto silently merges into an existing directory, which
    would violate the restore move-aside invariant.
    """
    src = Path(src)
    dst = Path(dst)
    # lexists: a dangling symlink at dst still counts as "exists".
    if os.path.lexists(dst):
        raise FileExistsError(f"ditto_copy destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([_DITTO, str(src), str(dst)])


def lstat_walk(root: Path) -> Iterator[Tuple[Path, os.stat_result]]:
    """Yield ``(path, lstat_result)`` for *root* and everything beneath it.

    Never follows symlinks: a symlink (even to a directory, even *root*
    itself) is yielded as the link and not descended into. Entries are
    yielded in sorted order for deterministic manifests.
    """
    root = Path(root)
    root_st = os.lstat(root)
    yield root, root_st
    if not stat.S_ISDIR(root_st.st_mode):
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        base = Path(dirpath)
        for name in sorted(dirnames + filenames):
            path = base / name
            yield path, os.lstat(path)


def tree_size(root: Path) -> int:
    """Total size in bytes of all non-directory entries under *root*.

    Symlinks contribute their own (lstat) size; targets are never followed.
    """
    total = 0
    for _path, st in lstat_walk(root):
        if not stat.S_ISDIR(st.st_mode):
            total += st.st_size
    return total


def sha256_file(path: Path) -> str:
    """Hex SHA-256 of a regular file, streamed in chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root: Path) -> List[dict]:
    """Build the manifest ``files`` entries for the tree rooted at *root*.

    Entry shapes (matching the manifest schema exactly):

    * regular file: ``{"relpath", "type": "file", "size", "sha256"}``
      (a single-file *root* yields one entry with relpath ``"."``)
    * symlink: ``{"relpath", "type": "symlink", "target"}`` — no hash
    * directory: ``{"relpath", "type": "dir"}`` (root dir itself omitted;
      recorded so empty directories survive verify/round-trip)
    """
    root = Path(root)
    entries: List[dict] = []
    for path, st in lstat_walk(root):
        relpath = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISLNK(st.st_mode):
            entries.append({
                "relpath": relpath,
                "type": "symlink",
                "target": os.readlink(path),
            })
        elif stat.S_ISDIR(st.st_mode):
            if relpath != ".":
                entries.append({"relpath": relpath, "type": "dir"})
        elif stat.S_ISREG(st.st_mode):
            entries.append({
                "relpath": relpath,
                "type": "file",
                "size": st.st_size,
                "sha256": sha256_file(path),
            })
        # Sockets/FIFOs/devices are not meaningful backup payload; skip.
    return entries


def to_portable(path: Path, home: Optional[Path] = None) -> str:
    """Serialize *path* for the manifest: ``~/rel`` inside home, else absolute.

    Purely lexical — no symlink resolution, so the stored path is exactly
    what was scanned.
    """
    if home is None:
        home = Path.home()
    path = Path(path)
    home = Path(home)
    try:
        rel = path.relative_to(home)
    except ValueError:
        return str(path)
    if rel == Path("."):
        return "~"
    return "~/" + rel.as_posix()


def from_portable(portable: str, home: Optional[Path] = None) -> Path:
    """Expand a manifest path against the *current* home directory."""
    if home is None:
        home = Path.home()
    if portable == "~":
        return Path(home)
    if portable.startswith("~/"):
        return Path(home) / portable[2:]
    return Path(portable)


def atomic_write_json(path: Path, obj) -> None:
    """Write *obj* as JSON to *path* atomically (temp file + ``os.replace``).

    The temp file lives in the same directory so the rename cannot cross
    filesystems. Data is fsynced before the rename; the file that appears at
    *path* is always complete.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def free_space(path: Path) -> int:
    """Free bytes available to this user on the volume containing *path*.

    Walks up to the nearest existing ancestor if *path* does not exist yet.
    """
    probe = Path(path)
    while not os.path.lexists(probe):
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    st = os.statvfs(probe)
    return st.f_bavail * st.f_frsize


def dest_fidelity_check(dest_dir: Path) -> List[str]:
    """Verify the destination filesystem preserves xattrs and symlinks.

    Creates a scratch directory inside *dest_dir*, writes an xattr with the
    ``xattr`` command and reads it back, and creates a symlink and lstats it
    back. Returns a list of human-readable problems; empty means the
    destination is fidelity-safe. All probe files are removed.
    """
    dest_dir = Path(dest_dir)
    problems: List[str] = []
    try:
        probe_dir = Path(tempfile.mkdtemp(prefix=".ultrabackup-fidelity-", dir=str(dest_dir)))
    except OSError as exc:
        return [f"destination is not writable: {dest_dir} ({exc})"]

    try:
        # xattr write + readback
        probe_file = probe_dir / "xattr-probe"
        attr_name = "ultrabackup.fidelity"
        attr_value = _uuid.uuid4().hex
        try:
            probe_file.write_text("ultrabackup fidelity probe\n", encoding="utf-8")
            wrote = run([_XATTR, "-w", attr_name, attr_value, str(probe_file)], check=False)
            if wrote.returncode != 0:
                problems.append(
                    "destination does not accept extended attributes "
                    f"(xattr -w failed: {(wrote.stderr or '').strip() or 'unknown error'})"
                )
            else:
                read = run([_XATTR, "-p", attr_name, str(probe_file)], check=False)
                if read.returncode != 0 or read.stdout.strip() != attr_value:
                    problems.append(
                        "extended attribute written to destination did not read back intact"
                    )
        except OSError as exc:
            problems.append(f"could not create probe file in destination: {exc}")

        # symlink create + lstat readback
        link = probe_dir / "symlink-probe"
        target = "ultrabackup-symlink-target"
        try:
            os.symlink(target, link)
            st = os.lstat(link)
            if not stat.S_ISLNK(st.st_mode) or os.readlink(link) != target:
                problems.append("symlink created in destination did not read back as a symlink")
        except OSError as exc:
            problems.append(f"destination does not support symlinks ({exc})")
    finally:
        _rmtree_no_follow(probe_dir)

    return problems


def _rmtree_no_follow(root: Path) -> None:
    """Best-effort removal of a scratch tree without following symlinks."""
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
            for name in filenames + dirnames:
                path = os.path.join(dirpath, name)
                try:
                    if os.path.isdir(path) and not os.path.islink(path):
                        os.rmdir(path)
                    else:
                        os.unlink(path)
                except OSError:
                    pass
        os.rmdir(root)
    except OSError:
        pass


def hardware_uuid() -> str:
    """This Mac's IOPlatformUUID, or ``"unknown"`` if it cannot be read."""
    result = run([_IOREG, "-rd1", "-c", "IOPlatformExpertDevice"], check=False)
    if result.returncode == 0 and result.stdout:
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', result.stdout)
        if match:
            return match.group(1)
    return "unknown"


def macos_version() -> str:
    """macOS product version (e.g. ``"15.5"``), or ``"unknown"``."""
    result = run([_SW_VERS, "-productVersion"], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "unknown"
