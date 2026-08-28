"""Round-trip and invariant tests for UltraBackup (SPEC.md test section).

All seven mandatory cases run against a fake home inside a tempdir with
``restore.system_calls_enabled = False``; nothing on the live system is
mutated (probes of /Library paths are read-only lstats, ``ditto`` only ever
copies between paths inside the tempdir).

Fixture (per SPEC): fake .app bundle with a hand-written XML Info.plist, an
Application Support subtree, a Preferences plist, a container whose
``Data/Documents`` symlink points to a real directory OUTSIDE the container,
plus a ``~/.fake`` dotfile wired in as a directly-constructed discovery item.
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ultrabackup import backup as backup_module  # noqa: E402
from ultrabackup import cli, discovery, fsutil  # noqa: E402
from ultrabackup import restore as restore_module  # noqa: E402

APP_NAME = "FakeApp"
BUNDLE_ID = "com.example.ultrabackup-fakeapp"

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key>
    <string>{bundle_id}</string>
    <key>CFBundleName</key>
    <string>{name}</string>
    <key>CFBundleShortVersionString</key>
    <string>1.2.3</string>
</dict>
</plist>
""".format(bundle_id=BUNDLE_ID, name=APP_NAME)

PREFS_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>theme</key>
    <string>dark</string>
</dict>
</plist>
"""


def _write(path: Path, content: str, mode: int = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mode is not None:
        os.chmod(path, mode)


class RoundTripTests(unittest.TestCase):
    """The 7 mandatory SPEC test cases, against an injected fake home."""

    def setUp(self):
        self._orig_syscalls = restore_module.system_calls_enabled
        restore_module.system_calls_enabled = False
        self.root = Path(tempfile.mkdtemp(prefix="ultrabackup-test-"))
        self.addCleanup(self._cleanup)

        self.home = self.root / "home"
        self.outside = self.root / "outside-docs"  # real dir OUTSIDE container & home
        self.dest = self.root / "backups"
        self._build_fixture()

        self.app = discovery.AppInfo(
            name=APP_NAME,
            path=self.bundle,
            bundle_id=BUNDLE_ID,
            version="1.2.3",
            helpers=[],
            mas_receipt=False,
        )
        self.items = self._discover_items()

    def _cleanup(self):
        restore_module.system_calls_enabled = self._orig_syscalls
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixture -----------------------------------------------------------

    def _build_fixture(self):
        home = self.home

        # Fake .app bundle with a hand-written XML Info.plist.
        self.bundle = home / "Applications" / (APP_NAME + ".app")
        _write(self.bundle / "Contents" / "Info.plist", INFO_PLIST)
        self.macos_bin = self.bundle / "Contents" / "MacOS" / APP_NAME
        _write(self.macos_bin, "#!/bin/sh\nexit 0\n", 0o755)

        # Application Support subtree with non-default permissions.
        self.app_support = home / "Library" / "Application Support" / APP_NAME
        _write(self.app_support / "data" / "notes.txt", "notes v1\n")
        _write(self.app_support / "secret.txt", "s3cret\n", 0o600)
        os.chmod(self.app_support, 0o750)

        # Preferences plist.
        self.prefs = home / "Library" / "Preferences" / (BUNDLE_ID + ".plist")
        _write(self.prefs, PREFS_PLIST)

        # Container: Data/Documents symlink -> real dir OUTSIDE the container.
        self.container = home / "Library" / "Containers" / BUNDLE_ID
        data = self.container / "Data"
        _write(data / "settings.json", '{"theme": "dark"}\n')
        _write(data / "nested" / "deep.txt", "deep\n")
        _write(data / restore_module.CONTAINER_METADATA_PLIST, "container metadata\n")
        self.outside.mkdir(parents=True)
        _write(self.outside / "SECRET.txt", "outside secret\n")
        os.symlink(str(self.outside), data / "Documents")
        # Second redirect symlink pointing INTO the home proper.
        self.home_docs = home / "Documents"
        _write(self.home_docs / "home-secret.txt", "home secret\n")
        os.symlink(str(self.home_docs), data / "HomeDocs")

        # Dotfile.
        self.dotfile = home / ".fake"
        _write(self.dotfile, '{"token": "abc"}\n', 0o600)

        self.dest.mkdir(parents=True)

        # Age every mtime so nothing accidentally reads as newer than the
        # backup's second-resolution created_at.
        old = time.time() - 3600
        for path, _st in fsutil.lstat_walk(home):
            try:
                os.utime(path, (old, old), follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass

    def _discover_items(self):
        items = discovery.discover(self.app, home=self.home)
        # ~/.fake dotfile wired in as a directly-constructed item (the real
        # known_apps.json has no entry for the fake app).
        items.append({
            "id": "%04d" % (len(items) + 1),
            "category": "dotfiles",
            "path": str(self.dotfile),
            "type": "file",
            "ownership": "user",
            "provenance": "extras",
            "status": "found",
        })
        found = {i["path"] for i in items if i["status"] == "found"}
        for path in (self.bundle, self.app_support, self.prefs,
                     self.container, self.dotfile):
            self.assertIn(str(path), found,
                          "fixture path not discovered as found: %s" % path)
        return items

    # -- helpers -----------------------------------------------------------

    def _backup(self):
        result = backup_module.do_backup(self.app, self.items, self.dest,
                                         home=self.home)
        self.assertFalse(result["partial"], result["manifest"]["completeness"])
        self.assertTrue((result["backup_dir"] / "manifest.json").is_file())
        return result["backup_dir"], result["manifest"]

    def _delete_originals(self):
        shutil.rmtree(self.bundle)
        shutil.rmtree(self.app_support)
        os.remove(self.prefs)
        shutil.rmtree(self.container)  # rmtree removes symlinks, never follows
        os.remove(self.dotfile)

    def _plan_and_apply(self, backup_dir, **kwargs):
        manifest = restore_module.load_backup(backup_dir)
        plan = restore_module.plan_restore(manifest, backup_dir, home=self.home)
        result = restore_module.apply_restore(plan, backup_dir, home=self.home,
                                              **kwargs)
        return plan, result

    def _snapshot(self, root, exclude_names=()):
        """Content+mode snapshot of a tree (mtimes excluded), lstat only."""
        entries = {}
        for path, st in fsutil.lstat_walk(root):
            rel = "." if path == root else path.relative_to(root).as_posix()
            if any(part in exclude_names for part in rel.split("/")):
                continue
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ("symlink", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                entries[rel] = ("dir", stat.S_IMODE(st.st_mode))
            elif stat.S_ISREG(st.st_mode):
                entries[rel] = ("file", stat.S_IMODE(st.st_mode),
                                fsutil.sha256_file(path))
        return entries

    def _mode(self, path):
        return stat.S_IMODE(os.lstat(path).st_mode)

    # -- 1. round-trip -----------------------------------------------------

    def test_1_roundtrip_restores_identical_content_and_permissions(self):
        expected = {
            "bundle": fsutil.hash_tree(self.bundle),
            "app_support": fsutil.hash_tree(self.app_support),
            "prefs": fsutil.hash_tree(self.prefs),
            "dotfile": fsutil.hash_tree(self.dotfile),
            "container": fsutil.hash_tree(self.container),
        }
        backup_dir, _manifest = self._backup()
        self._delete_originals()

        _plan, result = self._plan_and_apply(backup_dir)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["rolled_back"])

        # Content identical (hashes) for full-fidelity items.
        self.assertEqual(fsutil.hash_tree(self.bundle), expected["bundle"])
        self.assertEqual(fsutil.hash_tree(self.app_support), expected["app_support"])
        self.assertEqual(fsutil.hash_tree(self.prefs), expected["prefs"])
        self.assertEqual(fsutil.hash_tree(self.dotfile), expected["dotfile"])

        # Container: identical minus the containermanagerd metadata plist and
        # the home-redirect symlink, which the restore rightly excludes.
        excluded = {
            "Data/" + restore_module.CONTAINER_METADATA_PLIST,
            "Data/HomeDocs",
        }
        expected_container = [e for e in expected["container"]
                              if e["relpath"] not in excluded]
        self.assertEqual(fsutil.hash_tree(self.container), expected_container)

        # Permissions preserved.
        self.assertEqual(self._mode(self.app_support), 0o750)
        self.assertEqual(self._mode(self.app_support / "secret.txt"), 0o600)
        self.assertEqual(self._mode(self.macos_bin), 0o755)
        self.assertEqual(self._mode(self.dotfile), 0o600)

    # -- 2. symlink invariant ---------------------------------------------

    def test_2_symlink_invariant_target_dir_never_enters_payload(self):
        backup_dir, manifest = self._backup()

        citem = next(i for i in manifest["items"]
                     if i["category"] == "containers" and i["status"] == "copied")
        payload = backup_dir / "payload" / citem["id"] / BUNDLE_ID

        # Payload keeps Data/Documents as a symlink to the outside dir.
        link = payload / "Data" / "Documents"
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), str(self.outside))

        # The manifest records it as a symlink (no hash, with target).
        files = {f["relpath"]: f for f in citem["files"]}
        self.assertEqual(files["Data/Documents"]["type"], "symlink")
        self.assertEqual(files["Data/Documents"]["target"], str(self.outside))
        self.assertNotIn("sha256", files["Data/Documents"])

        # The pointed-at real dirs NEVER enter the payload.
        payload_names = {p.name for p, _st in
                         fsutil.lstat_walk(backup_dir / "payload")}
        self.assertNotIn("SECRET.txt", payload_names)
        self.assertNotIn("home-secret.txt", payload_names)

        # Restored container keeps Data/Documents as a symlink.
        shutil.rmtree(self.container)
        _plan, result = self._plan_and_apply(backup_dir)
        self.assertTrue(result["ok"], result)
        restored_link = self.container / "Data" / "Documents"
        self.assertTrue(os.path.islink(restored_link))
        self.assertEqual(os.readlink(restored_link), str(self.outside))
        # Home-redirect symlink excluded from restore; real dirs untouched.
        self.assertFalse(os.path.lexists(self.container / "Data" / "HomeDocs"))
        self.assertEqual((self.outside / "SECRET.txt").read_text(),
                         "outside secret\n")
        self.assertEqual((self.home_docs / "home-secret.txt").read_text(),
                         "home secret\n")

    # -- 3. dry-run --------------------------------------------------------

    def test_3_restore_without_apply_mutates_nothing(self):
        backup_dir, _manifest = self._backup()
        before = self._snapshot(self.home)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["restore", str(backup_dir)], home=self.home)

        self.assertEqual(rc, 0, "dry-run restore failed: %s" % err.getvalue())
        self.assertEqual(self._snapshot(self.home), before)
        self.assertFalse((backup_dir / restore_module.JOURNAL_NAME).exists(),
                         "dry-run must not create a restore journal")
        self.assertIn("Dry-run", out.getvalue())

    # -- 4. conflict -> move-aside; rollback -------------------------------

    def test_4_conflict_moves_aside_and_rollback_restores_prior_state(self):
        backup_dir, _manifest = self._backup()

        live_pref = "live prefs content\n"
        live_dot = "live dotfile content\n"
        self.prefs.write_text(live_pref)
        self.dotfile.write_text(live_dot)
        future = time.time() + 300
        os.utime(self.prefs, (future, future))
        os.utime(self.dotfile, (future, future))
        before_apply = self._snapshot(
            self.home, exclude_names={restore_module.PRERESTORE_DIRNAME})

        manifest = restore_module.load_backup(backup_dir)
        plan = restore_module.plan_restore(manifest, backup_dir, home=self.home)
        pentry = next(e for e in plan if Path(e["target"]) == self.prefs)
        self.assertTrue(pentry["conflict"])
        self.assertTrue(pentry["live_newer"])

        result = restore_module.apply_restore(
            plan, backup_dir, home=self.home, overwrite_newer=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["moved_aside"])

        # Backup content is live again; the live version went to move-aside.
        self.assertEqual(self.prefs.read_text(), PREFS_PLIST)
        self.assertEqual(self.dotfile.read_text(), '{"token": "abc"}\n')
        aside_recs = [r for r in result["moved_aside"]
                      if Path(r["original"]) == self.prefs]
        self.assertEqual(len(aside_recs), 1)
        aside = Path(aside_recs[0]["aside"])
        self.assertIn(restore_module.PRERESTORE_DIRNAME, aside.parts)
        self.assertEqual(aside.read_text(), live_pref)

        # Rollback restores the pre-restore state.
        rb = restore_module.rollback(backup_dir, home=self.home)
        self.assertTrue(rb["ok"], rb)
        self.assertTrue(rb["complete"], rb)
        self.assertEqual(self.prefs.read_text(), live_pref)
        self.assertEqual(self.dotfile.read_text(), live_dot)
        after = self._snapshot(
            self.home, exclude_names={restore_module.PRERESTORE_DIRNAME})
        self.assertEqual(after, before_apply)

        # A second rollback must not re-undo anything (journal marked).
        rb2 = restore_module.rollback(backup_dir, home=self.home)
        self.assertTrue(rb2["ok"], rb2)
        self.assertEqual(rb2["undone"], 0)
        self.assertEqual(self.prefs.read_text(), live_pref)

    # -- 5. manifest atomicity --------------------------------------------

    def test_5_payload_without_manifest_fails_load_clearly(self):
        backup_dir, _manifest = self._backup()
        (backup_dir / "manifest.json").unlink()
        self.assertTrue((backup_dir / "payload").is_dir())

        with self.assertRaises(FileNotFoundError) as ctx:
            restore_module.load_backup(backup_dir)
        self.assertIn("manifest.json", str(ctx.exception))

        # Corrupt manifest also fails clearly (ValueError, not a crash).
        (backup_dir / "manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            restore_module.load_backup(backup_dir)

    # -- 6. verify detects tampering ---------------------------------------

    def test_6_verify_detects_tampered_payload(self):
        backup_dir, manifest = self._backup()

        clean = restore_module.verify(backup_dir)
        self.assertTrue(clean["ok"], clean["mismatches"])

        pitem = next(i for i in manifest["items"]
                     if i["category"] == "preferences" and i["status"] == "copied")
        payload_file = backup_dir / "payload" / pitem["id"] / self.prefs.name
        original = payload_file.read_bytes()
        # Same size, different bytes: only the hash can catch it.
        payload_file.write_bytes(original[:-1] + (b"X" if original[-1:] != b"X" else b"Y"))

        tampered = restore_module.verify(backup_dir)
        self.assertFalse(tampered["ok"])
        self.assertTrue(
            any(m["reason"] == "sha256 mismatch" and m["item_id"] == pitem["id"]
                for m in tampered["mismatches"]),
            tampered["mismatches"],
        )

    # -- 7. live_newer without --overwrite-newer -> skip --------------------

    def test_7_live_newer_without_overwrite_newer_skips(self):
        backup_dir, _manifest = self._backup()

        live = "live-newer prefs content\n"
        self.prefs.write_text(live)
        future = time.time() + 300
        os.utime(self.prefs, (future, future))

        manifest = restore_module.load_backup(backup_dir)
        plan = restore_module.plan_restore(manifest, backup_dir, home=self.home)
        entry = next(e for e in plan if Path(e["target"]) == self.prefs)
        self.assertTrue(entry["live_newer"])
        self.assertEqual(entry["action"], "restore")  # plan marks, apply skips

        result = restore_module.apply_restore(plan, backup_dir, home=self.home)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.prefs.read_text(), live,
                         "live-newer target must not be overwritten")
        skipped = [s for s in result["skipped"]
                   if Path(s["target"]) == self.prefs]
        self.assertEqual(len(skipped), 1, result["skipped"])
        self.assertIn("newer", skipped[0]["reason"])
        restored_targets = {r["target"] for r in result["restored"]}
        self.assertNotIn(str(self.prefs), restored_targets)

    # -- 8. top-level symlink item: stored and restored as a link -----------

    def test_8_top_level_symlink_item_never_dittoed(self):
        link = self.home / ".fakelink"
        os.symlink(str(self.outside), link)
        items = list(self.items) + [{
            "id": "%04d" % (len(self.items) + 1),
            "category": "dotfiles",
            "path": str(link),
            "type": "symlink",
            "ownership": "user",
            "provenance": "extras",
            "status": "found",
        }]
        result = backup_module.do_backup(self.app, items, self.dest,
                                         home=self.home)
        self.assertFalse(result["partial"], result["manifest"]["completeness"])
        backup_dir, manifest = result["backup_dir"], result["manifest"]

        litem = next(i for i in manifest["items"]
                     if i["original_path"] == "~/.fakelink")
        self.assertEqual(litem["type"], "symlink")
        self.assertEqual(litem["status"], "copied")
        self.assertEqual(
            litem["files"],
            [{"relpath": ".", "type": "symlink", "target": str(self.outside)}],
        )
        payload = backup_dir / "payload" / litem["id"] / ".fakelink"
        self.assertTrue(os.path.islink(payload),
                        "payload for a symlink item must itself be a symlink")
        self.assertEqual(os.readlink(payload), str(self.outside))
        # The pointed-to tree NEVER enters the payload (SPEC invariant 1).
        payload_names = {p.name for p, _st in
                         fsutil.lstat_walk(backup_dir / "payload")}
        self.assertNotIn("SECRET.txt", payload_names)
        self.assertTrue(restore_module.verify(backup_dir)["ok"])

        # Round-trip: the whole restore succeeds and the link comes back.
        os.remove(link)
        manifest = restore_module.load_backup(backup_dir)
        plan = restore_module.plan_restore(manifest, backup_dir, home=self.home)
        result = restore_module.apply_restore(plan, backup_dir, home=self.home)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["rolled_back"])
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), str(self.outside))

    # -- 9. corrupt/malicious manifest paths are never restored -------------

    def test_9_unsafe_manifest_entries_are_skipped(self):
        backup_dir, _manifest = self._backup()
        mpath = backup_dir / "manifest.json"
        data = json.loads(mpath.read_text(encoding="utf-8"))
        outside_target = self.root / "outside-target"
        data["items"][0]["original_path"] = "~/../../etc/ultrabackup-evil"
        data["items"][1]["original_path"] = str(outside_target)  # ownership "user"
        data["items"][2]["id"] = "../../../../etc"
        mpath.write_text(json.dumps(data), encoding="utf-8")

        manifest = restore_module.load_backup(backup_dir)
        plan = restore_module.plan_restore(manifest, backup_dir, home=self.home)
        for i in range(3):
            self.assertEqual(plan[i]["action"], "skip", plan[i])
            self.assertIn("unsafe manifest entry", plan[i]["reason"])

        result = restore_module.apply_restore(plan, backup_dir, home=self.home)
        self.assertTrue(result["ok"], result)
        self.assertFalse(os.path.lexists(outside_target))
        restored_targets = {r["target"] for r in result["restored"]}
        for i in range(3):
            self.assertNotIn(str(plan[i]["target"]), restored_targets)

    # -- 10. rollback removes directories the restore created ---------------

    def test_10_rollback_removes_created_container_dirs(self):
        backup_dir, _manifest = self._backup()
        self._delete_originals()
        before = self._snapshot(
            self.home, exclude_names={restore_module.PRERESTORE_DIRNAME})

        _plan, result = self._plan_and_apply(backup_dir)
        self.assertTrue(result["ok"], result)
        self.assertTrue((self.container / "Data").is_dir())

        rb = restore_module.rollback(backup_dir, home=self.home)
        self.assertTrue(rb["ok"], rb)
        self.assertTrue(rb["complete"], rb)
        self.assertFalse(
            os.path.lexists(self.container),
            "rollback must remove container dirs created by the restore",
        )
        after = self._snapshot(
            self.home, exclude_names={restore_module.PRERESTORE_DIRNAME})
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
