"""App and data discovery for UltraBackup.

Locates installed applications, their helper bundles, and every on-disk data
location from the category table in SPEC.md — without copying anything.

Invariants honored here:
- symlinks are never followed (``os.lstat`` / ``os.path.islink`` only);
- every subprocess uses an argument list, never ``shell=True``;
- functions that touch the user home accept ``home`` for testability;
- ``mdfind`` is used only to locate a ``.app`` by bundle identifier, never
  to enumerate ``~/Library`` (empty mdfind output does not mean "absent").
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_KNOWN_APPS_FILE = Path(__file__).resolve().parent / "known_apps.json"

# Helper bundles live in these locations inside an app bundle.
_HELPER_LOCATIONS = (
    ("Frameworks", "*.app"),
    ("XPCServices", "*.xpc"),
    (os.path.join("Library", "LoginItems"), "*.app"),
)


class AppNotFoundError(LookupError):
    """Raised by :func:`find_app` when a query cannot be resolved to an app."""


@dataclass
class AppInfo:
    """Resolved application identity.

    ``path`` is ``None`` for known apps with no installed ``.app`` bundle
    (CLI-only tools such as Claude Code); ``bundle_id`` then comes from the
    ``known_apps.json`` entry's ``match_bundle_ids[0]``.
    """

    name: str
    path: Optional[Path]
    bundle_id: Optional[str]
    version: Optional[str] = None
    helpers: List[dict] = field(default_factory=list)
    mas_receipt: bool = False


# ---------------------------------------------------------------------------
# plist / subprocess helpers
# ---------------------------------------------------------------------------

def _read_info_plist(plist_path: Path) -> dict:
    """Parse a plist into a dict via ``plutil``; empty dict on any failure.

    Falls back to :mod:`plistlib` when ``plutil`` cannot produce JSON (e.g.
    plists containing ``data``/``date`` values, or plutil unavailable).
    """
    try:
        proc = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", "--", str(plist_path)],
            capture_output=True,
            check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        pass
    try:
        with open(plist_path, "rb") as fh:
            data = plistlib.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _entitlement_groups(bundle_path: Path) -> List[str]:
    """app-group ids from one bundle's code signature; [] on any failure."""
    try:
        cs = subprocess.run(
            ["codesign", "-d", "--entitlements", "-", "--xml", str(bundle_path)],
            capture_output=True,
            check=True,
        )
        xml = cs.stdout
        if not xml.strip():
            return []
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="ultrabackup-ent-", suffix=".plist", delete=False
            ) as tmp:
                tmp.write(xml)
                tmp_name = tmp.name
            pl = subprocess.run(
                ["plutil", "-convert", "json", "-o", "-", "--", tmp_name],
                capture_output=True,
                check=True,
            )
            data = json.loads(pl.stdout.decode("utf-8"))
        finally:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        groups = data.get("com.apple.security.application-groups", [])
        return [g for g in groups if isinstance(g, str)]
    except Exception:
        return []


def _mdfind_app_by_bundle_id(bundle_id: str) -> Optional[Path]:
    """Locate a .app via Spotlight by kMDItemCFBundleIdentifier, or None."""
    if not bundle_id or "'" in bundle_id or '"' in bundle_id:
        return None
    try:
        proc = subprocess.run(
            ["mdfind", "kMDItemCFBundleIdentifier == '%s'" % bundle_id],
            capture_output=True,
            check=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.endswith(".app") and os.path.isdir(line):
            return Path(line)
    return None


# ---------------------------------------------------------------------------
# known_apps.json
# ---------------------------------------------------------------------------

def _load_known_apps() -> dict:
    try:
        with open(_KNOWN_APPS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _known_entry_for(app: AppInfo) -> Optional[dict]:
    """Match an AppInfo to a known_apps entry by key, name, or bundle id."""
    name_l = (app.name or "").lower()
    for key, entry in _load_known_apps().items():
        if not isinstance(entry, dict):
            continue
        if name_l and name_l == key.lower():
            return entry
        if name_l and name_l in {n.lower() for n in entry.get("match_names", [])}:
            return entry
        if app.bundle_id and app.bundle_id in entry.get("match_bundle_ids", []):
            return entry
    return None


def _expand_portable(portable: str, home: Path) -> Path:
    """Expand a '~/'-prefixed path against the injected home, not Path.home()."""
    if portable == "~":
        return home
    if portable.startswith("~/"):
        return home / portable[2:]
    return Path(portable)


# ---------------------------------------------------------------------------
# App resolution
# ---------------------------------------------------------------------------

def _truecase_leaf(path: Path) -> Path:
    """Normalize the final path component to its on-disk spelling.

    macOS system volumes are case-insensitive, so a query like 'claude'
    resolves; the manifest must still record the real 'Claude.app' spelling
    or restore onto a case-sensitive volume would diverge.
    """
    try:
        for entry in os.scandir(path.parent):
            if entry.name.lower() == path.name.lower():
                return path.parent / entry.name
    except OSError:
        pass
    return path


def _app_info_from_bundle(bundle_path: Path) -> AppInfo:
    """Build an AppInfo from an installed .app bundle."""
    bundle_path = _truecase_leaf(Path(os.path.abspath(str(bundle_path))))
    info = _read_info_plist(bundle_path / "Contents" / "Info.plist")
    name = bundle_path.name
    if name.endswith(".app"):
        name = name[: -len(".app")]
    return AppInfo(
        name=name,
        path=bundle_path,
        bundle_id=info.get("CFBundleIdentifier"),
        version=info.get("CFBundleShortVersionString") or info.get("CFBundleVersion"),
        helpers=find_helpers(bundle_path),
        mas_receipt=os.path.lexists(bundle_path / "Contents" / "_MASReceipt"),
    )


def find_helpers(app_path: Path) -> List[dict]:
    """Enumerate helper bundles inside an app bundle.

    Scans ``Contents/Frameworks/*.app``, ``Contents/XPCServices/*.xpc`` and
    ``Contents/Library/LoginItems/*.app`` (one level, symlinked entries
    skipped) and reads each helper's bundle id from its ``Info.plist``.
    Electron helpers have their own ids with their own Preferences,
    HTTPStorages and Containers.

    Returns a list of ``{"bundle_id": str, "path": str}`` dicts; helpers
    whose bundle id cannot be read are omitted.
    """
    helpers: List[dict] = []
    contents = Path(app_path) / "Contents"
    for subdir, pattern in _HELPER_LOCATIONS:
        base = contents / subdir
        for bundle in _safe_glob(base, pattern):
            try:
                if os.path.islink(bundle):
                    continue
            except OSError:
                continue
            info = _read_info_plist(bundle / "Contents" / "Info.plist")
            hbid = info.get("CFBundleIdentifier")
            if hbid:
                helpers.append({"bundle_id": hbid, "path": str(bundle)})
    return helpers


def group_container_ids(app_path: Path) -> List[str]:
    """App-group container ids for the app and its helpers.

    Reads ``com.apple.security.application-groups`` from the code signature
    (``codesign -d --entitlements - --xml`` parsed via ``plutil -convert
    json``). Any failure is silent and contributes no ids.
    """
    ids: List[str] = []
    paths = [Path(app_path)]
    paths.extend(Path(h["path"]) for h in find_helpers(app_path))
    for path in paths:
        for gid in _entitlement_groups(path):
            if gid not in ids:
                ids.append(gid)
    return ids


def list_installed(applications_dir: Path = Path("/Applications")) -> List[AppInfo]:
    """AppInfo for every .app directly inside ``applications_dir``.

    Non-recursive; symlinked bundles are skipped (never followed) and
    unreadable bundles are ignored.
    """
    apps: List[AppInfo] = []
    try:
        entries = sorted(Path(applications_dir).iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(".app"):
            continue
        try:
            if os.path.islink(entry) or not entry.is_dir():
                continue
            apps.append(_app_info_from_bundle(entry))
        except Exception:
            continue
    return apps


def find_app(query: str, home: Path = None) -> AppInfo:
    """Resolve ``query`` to an AppInfo.

    Resolution order:
      1. explicit path to a ``.app`` bundle;
      2. exact name in ``/Applications`` (``<query>.app``);
      3. ``known_apps.json`` key (case-insensitive) — match_names in
         /Applications first, then mdfind per match_bundle_ids; an entry
         with no installed .app is still valid (CLI-only tool): ``path`` is
         None and ``bundle_id`` is ``match_bundle_ids[0]``;
      4. mdfind by ``kMDItemCFBundleIdentifier == query``.

    Raises :class:`AppNotFoundError` (a LookupError) with a clear message
    when nothing matches.
    """
    home = Path(home) if home is not None else Path.home()
    query = str(query)

    # 1. Explicit path to a .app bundle.
    if query.endswith(".app"):
        candidate = _expand_portable(query, home)
        if candidate.is_dir():
            return _app_info_from_bundle(candidate)

    # 2. Exact name in /Applications.
    base = query[: -len(".app")] if query.endswith(".app") else query
    candidate = Path("/Applications") / (base + ".app")
    if candidate.is_dir():
        return _app_info_from_bundle(candidate)

    # 3. known_apps.json key (case-insensitive).
    for key, entry in _load_known_apps().items():
        if not isinstance(entry, dict) or query.lower() != key.lower():
            continue
        for name in entry.get("match_names", []):
            candidate = Path("/Applications") / (name + ".app")
            if candidate.is_dir():
                return _app_info_from_bundle(candidate)
        for bid in entry.get("match_bundle_ids", []):
            found = _mdfind_app_by_bundle_id(bid)
            if found is not None:
                return _app_info_from_bundle(found)
        # No installed .app: still a valid target (CLI-only tool).
        bids = entry.get("match_bundle_ids", [])
        names = entry.get("match_names", [])
        return AppInfo(
            name=names[0] if names else key,
            path=None,
            bundle_id=bids[0] if bids else None,
            version=None,
            helpers=[],
            mas_receipt=False,
        )

    # 4. mdfind fallback: treat the query as a bundle identifier.
    found = _mdfind_app_by_bundle_id(query)
    if found is not None:
        return _app_info_from_bundle(found)

    raise AppNotFoundError(
        "App '%s' not found: not an existing .app path, no /Applications/%s.app, "
        "no known_apps.json entry, and Spotlight (mdfind) found no bundle with "
        "that identifier. Tip: pass the full path to the .app bundle." % (query, base)
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _safe_glob(directory: Path, pattern: str) -> List[Path]:
    """Sorted non-recursive glob; empty list on any OS error."""
    try:
        return sorted(Path(directory).glob(pattern))
    except OSError:
        return []


def _probe(path: Path, expected_type: str) -> "tuple[str, str]":
    """(status, type) for a path without following symlinks.

    PermissionError on lstat/access (or a False os.access) means
    ``permission_denied``; the type of an unreadable/absent path falls back
    to ``expected_type``.
    """
    try:
        st = os.lstat(path)
    except PermissionError:
        return "permission_denied", expected_type
    except OSError:
        return "missing", expected_type
    if stat.S_ISLNK(st.st_mode):
        # The link itself is readable via readlink; do not follow it.
        return "found", "symlink"
    ptype = "dir" if stat.S_ISDIR(st.st_mode) else "file"
    try:
        readable = os.access(path, os.R_OK)
    except PermissionError:
        readable = False
    if not readable:
        return "permission_denied", ptype
    return "found", ptype


def _ownership(path: Path, home: Path) -> str:
    """Symbolic ownership from the real owner: 'root' iff lstat uid is 0.

    /Applications bundles are commonly owned by the installing user and must
    stay backupable/restorable without sudo. A path that cannot be lstat'ed
    falls back to the location rule (outside the home -> 'root'), which is
    the conservative choice for restore-time gating.
    """
    try:
        return "root" if os.lstat(str(path)).st_uid == 0 else "user"
    except OSError:
        pass
    try:
        Path(os.path.abspath(str(path))).relative_to(Path(os.path.abspath(str(home))))
        return "user"
    except ValueError:
        return "root"


def discover(app: AppInfo, home: Path = None, include_caches: bool = False) -> List[dict]:
    """Build the item list for every discovery category — copying nothing.

    Each item: ``{"id", "category", "path", "type", "ownership",
    "provenance", "status"}`` with sequential zero-padded ids, absolute
    string paths, ownership taken from the real owner ('root' iff uid 0),
    and status 'found' | 'missing' | 'permission_denied'.

    Fixed-path candidates are always emitted (probed for status); glob
    categories emit only their matches. ``caches`` is emitted only when
    ``include_caches`` is true.
    """
    home = Path(home) if home is not None else Path.home()
    lib = home / "Library"
    name = app.name
    bid = app.bundle_id or None
    helper_bids: List[str] = []
    for helper in app.helpers or []:
        hbid = helper.get("bundle_id")
        if hbid and hbid != bid and hbid not in helper_bids:
            helper_bids.append(hbid)
    # (bundle_id, provenance) pairs for per-bundle-id categories.
    bid_sets = ([(bid, "template")] if bid else []) + [
        (hbid, "helper") for hbid in helper_bids
    ]

    raw: List[dict] = []
    seen: set = set()

    def add(category: str, path: Path, provenance: str, expected_type: str) -> None:
        apath = Path(os.path.abspath(str(path)))
        key = str(apath)
        if key in seen:
            return
        seen.add(key)
        status, ptype = _probe(apath, expected_type)
        raw.append(
            {
                "category": category,
                "path": str(apath),
                "type": ptype,
                "ownership": _ownership(apath, home),
                "provenance": provenance,
                "status": status,
            }
        )

    # app_bundle
    bundle_path = app.path if app.path is not None else Path("/Applications") / (name + ".app")
    add("app_bundle", bundle_path, "template", "dir")

    # app_support
    add("app_support", lib / "Application Support" / name, "template", "dir")
    if bid:
        add("app_support", lib / "Application Support" / bid, "template", "dir")

    # preferences: <bid>.plist, <bid>.*.plist, ByHost/<bid>.*.plist — app + helpers
    prefs = lib / "Preferences"
    byhost = prefs / "ByHost"
    for pbid, prov in bid_sets:
        add("preferences", prefs / (pbid + ".plist"), prov, "file")
        for match in _safe_glob(prefs, pbid + ".*.plist"):
            add("preferences", match, prov, "file")
        for match in _safe_glob(byhost, pbid + ".*.plist"):
            add("preferences", match, prov, "file")

    # containers — app + helpers
    for pbid, prov in bid_sets:
        add("containers", lib / "Containers" / pbid, prov, "dir")

    # group_containers — ids from entitlements (app + helpers)
    if app.path is not None:
        for gid in group_container_ids(app.path):
            add("group_containers", lib / "Group Containers" / gid, "entitlements", "dir")

    # saved_state
    if bid:
        add("saved_state", lib / "Saved Application State" / (bid + ".savedState"), "template", "dir")

    # http_storages — app + helpers
    for pbid, prov in bid_sets:
        add("http_storages", lib / "HTTPStorages" / pbid, prov, "dir")

    # webkit
    if bid:
        add("webkit", lib / "WebKit" / bid, "template", "dir")

    # cookies
    if bid:
        add("cookies", lib / "Cookies" / (bid + ".binarycookies"), "template", "file")

    # launch_agents — user LaunchAgents, system LaunchAgents/LaunchDaemons
    if bid:
        for agents_dir in (
            lib / "LaunchAgents",
            Path("/Library/LaunchAgents"),
            Path("/Library/LaunchDaemons"),
        ):
            for match in _safe_glob(agents_dir, bid + "*.plist"):
                add("launch_agents", match, "template", "file")

    # system_support
    add("system_support", Path("/Library/Application Support") / name, "template", "dir")

    # logs
    add("logs", lib / "Logs" / name, "template", "dir")
    if bid:
        add("logs", lib / "Logs" / bid, "template", "dir")

    # app_scripts
    if bid:
        add("app_scripts", lib / "Application Scripts" / bid, "template", "dir")

    # caches — opt-in only
    if include_caches:
        add("caches", lib / "Caches" / name, "template", "dir")
        if bid:
            add("caches", lib / "Caches" / bid, "template", "dir")

    # dotfiles — curated extras from known_apps.json
    entry = _known_entry_for(app)
    if entry:
        for extra in entry.get("extras", []):
            path = _expand_portable(extra, home)
            expected = "file" if path.suffix else "dir"
            add("dotfiles", path, "extras", expected)

    items: List[dict] = []
    for index, item in enumerate(raw, start=1):
        ordered = {"id": "%04d" % index}
        ordered.update(item)
        items.append(ordered)
    return items
