import json
import io
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from modsync.downgrade import engine, recipe, tools
from tests.test_gameversion import fake_pe

HAVE_TOOLS = tools.find_xdelta3() is not None and tools.find_extractor() is not None
HAVE_7Z_CREATE = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


class RecipeIndexTests(unittest.TestCase):
    def setUp(self):
        self.idx = recipe.load_index(refresh=False)

    def test_bundled_loads(self):
        self.assertIn(self.idx.origin, ("cached", "bundled"))
        self.assertEqual(self.idx.game_appid, 489830)
        self.assertEqual(self.idx.exe_name, "SkyrimSE.exe")
        self.assertIn("1.6.1170", self.idx.targets)

    def test_archives_for_english_is_base_only(self):
        arcs = self.idx.archives_for("1.6.1170", "english")
        self.assertEqual(sorted(a.depot for a in arcs), ["489831", "489832", "489833"])
        self.assertTrue(all(a.language is None for a in arcs))

    def test_archives_for_language_adds_one_depot(self):
        arcs = self.idx.archives_for("1.6.1170", "german")
        depots = {a.depot: a for a in arcs}
        self.assertEqual(len(depots), 4)
        self.assertEqual(depots["489836"].language, "german")
        self.assertTrue(depots["489836"].parts[0].sha1)
        self.assertTrue(all(p.sha1 is None for p in depots["489836"].parts[1:]))

    def test_unknown_target(self):
        with self.assertRaises(recipe.RecipeError):
            self.idx.archives_for("9.9.9", "english")

    def test_steam_manifests_and_estimate(self):
        self.assertEqual(self.idx.steam_manifests("1.6.1170")["489833"], "1914580699073641964")
        self.assertIsNone(self.idx.steam_manifests("0.0.1"))
        self.assertGreater(self.idx.estimated_bytes("1.6.1170") or 0, 10**9)

    def test_stem(self):
        a = recipe.Archive("1", None, "489831.7z.001", ())
        self.assertEqual(a.stem, "489831.7z")
        self.assertEqual(recipe.Archive("1", None, "489833.7z", ()).stem, "489833.7z")

    def test_old_remote_index_falls_back_to_complete_bundled_recipe(self):
        old = json.loads(recipe.BUNDLED.read_text())
        old["schema"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(recipe.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(old).encode())):
                with patch.object(recipe, "cache_path", return_value=Path(tmp) / "index.json"):
                    idx = recipe.load_index()
        self.assertEqual(idx.origin, "bundled")
        self.assertIn("bink2w64.dll", idx.deletes_for("1.5.97"))


class DownloadTests(unittest.TestCase):
    def test_unhashed_interrupted_part_resumes_then_becomes_offline_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = recipe.Archive("1", None, "1.7z.001", (
                recipe.Part("https://example.test/001", None),
                recipe.Part("https://example.test/002", None),
            ))
            plan = engine.Plan(root, "new", "sha", "old", "english", [archive], "SkyrimSE.exe")
            first = engine._part_cache_path(root, archive, 0)
            first.parent.mkdir(parents=True)
            first.write_bytes(b"first")
            dest = engine._part_cache_path(root, archive, 1)
            short = io.BytesIO(b"abc")
            short.status = 200
            short.headers = {"Content-Length": "6"}
            with patch.object(engine.urllib.request, "urlopen", return_value=short):
                with self.assertRaisesRegex(engine.DowngradeError, "resume"):
                    engine.download_all(plan, root)
            self.assertFalse(dest.exists())
            resumed = io.BytesIO(b"def")
            resumed.status = 206
            resumed.headers = {"Content-Length": "3", "Content-Range": "bytes 3-5/6"}
            with patch.object(engine.urllib.request, "urlopen", return_value=resumed) as request:
                joined, moved = engine.download_all(plan, root)
            self.assertEqual(request.call_args.args[0].get_header("Range"), "bytes=3-")
            self.assertEqual(moved, 3)
            self.assertEqual(joined[0].read_bytes(), b"firstabcdef")
            with patch.object(engine.urllib.request, "urlopen", side_effect=AssertionError("network used")):
                self.assertEqual(engine.download_all(plan, root)[1], 0)

    def test_416_only_promotes_an_exact_complete_partial(self):
        for length, succeeds in ((6, True), (4, False)):
            with self.subTest(length=length), tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "archive"
                partial = dest.with_name("archive.part")
                partial.write_bytes(b"abcdef")
                error = urllib.error.HTTPError("https://example.test", 416, "range", {"Content-Range": f"bytes */{length}"}, None)
                with patch.object(engine.urllib.request, "urlopen", side_effect=error):
                    if succeeds:
                        self.assertEqual(engine._download(error.url, dest, None, "test"), 0)
                        self.assertEqual(dest.read_bytes(), b"abcdef")
                    else:
                        with self.assertRaises(engine.DowngradeError):
                            engine._download(error.url, dest, None, "test")
                        self.assertFalse(dest.exists())

    def test_legacy_unhashed_cache_is_not_trusted(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.write_bytes(b"complete")
            part = recipe.Part(source.as_uri(), None)
            archive = recipe.Archive("1", None, "1.7z", (part,))
            legacy = root / hashlib.sha1(part.url.encode()).hexdigest() / "1.7z"
            legacy.parent.mkdir()
            legacy.write_bytes(b"incomplete")
            plan = engine.Plan(root, "new", "sha", "old", "english", [archive], "SkyrimSE.exe")
            paths, moved = engine.download_all(plan, root)
            self.assertEqual(paths[0].read_bytes(), b"complete")
            self.assertEqual(moved, 8)


def _write_index(path: Path, from_sha1: str, url: str, sha1: str) -> None:
    doc = {
        "schema": 2,
        "game": {"appid": 489830, "exe": "SkyrimSE.exe"},
        "from": {"version": "1.7.104", "exe": "SkyrimSE.exe", "exe_sha1": from_sha1},
        "targets": {"1.6.1170": {"estimated_kib": 1, "depots": {
            "489831": {"archive": "489831.7z", "language": None, "parts": [{"url": url, "sha1": sha1}]},
            "489832": {"archive": "489832.7z", "language": None, "parts": [{"url": url, "sha1": sha1}]},
            "489833": {"archive": "489833.7z", "language": None, "parts": [{"url": url, "sha1": sha1}]},
        }}},
        "post_steps": ["remove_shader_cache", "reset_content_catalog"],
        "steam": {},
    }
    path.write_text(json.dumps(doc))


@unittest.skipUnless(HAVE_TOOLS and HAVE_7Z_CREATE, "needs xdelta3 and 7z")
class EngineTests(unittest.TestCase):
    """End-to-end on a fake game dir: build a real xdelta3 patch, pack it in a
    real 7z, serve it via file://, and run the whole pipeline."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.game = self.tmp / "Skyrim Special Edition"
        (self.game / "Data" / "ShaderCache").mkdir(parents=True)
        (self.game / "Data" / "ShaderCache" / "x.bin").write_bytes(b"cache")
        self.new_exe = fake_pe(1, 7, 104, 0) + b"NEW" * 5000
        self.old_exe = fake_pe(1, 6, 1170, 0) + b"OLD" * 5000
        (self.game / "SkyrimSE.exe").write_bytes(self.new_exe)
        (self.game / "Data" / "Skyrim.esm").write_bytes(b"esm-new" * 1000)
        old_esm = b"esm-old" * 1000

        # build patches: <src> -> <target>
        build = self.tmp / "build"
        (build / "Data").mkdir(parents=True)
        (build / "SkyrimSE.exe.old").write_bytes(self.old_exe)
        (build / "Data" / "Skyrim.esm.old").write_bytes(old_esm)
        xd = str(tools.find_xdelta3())
        subprocess.run([xd, "-e", "-f", "-s", str(self.game / "SkyrimSE.exe"), str(build / "SkyrimSE.exe.old"), str(build / "SkyrimSE.exe.xdelta")], check=True)
        subprocess.run([xd, "-e", "-f", "-s", str(self.game / "Data" / "Skyrim.esm"), str(build / "Data" / "Skyrim.esm.old"), str(build / "Data" / "Skyrim.esm.xdelta")], check=True)
        for extra in build.rglob("*.old"):
            extra.unlink()
        self.archive = self.tmp / "patch.7z"
        sevenz = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
        subprocess.run([sevenz, "a", "-bso0", str(self.archive), "SkyrimSE.exe.xdelta", "Data"], cwd=build, check=True)
        self.sha1 = engine.sha1_of(self.archive)

        self.prefix = self.tmp / "pfx"
        cat = self.prefix / "drive_c/users/steamuser/AppData/Local/Skyrim Special Edition"
        cat.mkdir(parents=True)
        (cat / "ContentCatalog.txt").write_text('{"AchievementSafe": true}')

        idx_path = self.tmp / "index.json"
        _write_index(idx_path, engine.sha1_of(self.game / "SkyrimSE.exe"), self.archive.as_uri(), self.sha1)
        self.index = recipe._parse(idx_path.read_text(), "test")
        self.cache = self.tmp / "cache"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_run(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        self.assertEqual(len(plan.archives), 3)
        events = []
        result = engine.run(plan, cache_dir=self.cache, prefix_dir=self.prefix, progress=events.append)

        self.assertEqual(result.installed_version, "1.6.1170")
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.old_exe)
        self.assertEqual((self.game / "Data" / "Skyrim.esm").read_bytes(), b"esm-old" * 1000)
        self.assertFalse((self.game / "Data" / "ShaderCache").exists())
        # The originals stay behind for `restore`; the scratch dirs do not.
        work = self.game / ".modsync-downgrade"
        self.assertEqual(result.backup_dir, work / "backup")
        self.assertEqual((work / "backup" / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertEqual((work / "backup" / "Data" / "Skyrim.esm").read_bytes(), b"esm-new" * 1000)
        self.assertTrue((work / "manifest.json").exists())
        self.assertFalse((work / "out").exists())
        self.assertFalse((work / "patches").exists())
        self.assertTrue(engine.has_backup(self.game))
        with self.assertRaisesRegex(engine.DowngradeError, "modsync game restore"):
            engine.preflight(plan)
        cat_dir = self.prefix / "drive_c/users/steamuser/AppData/Local/Skyrim Special Edition"
        self.assertTrue((cat_dir / "ContentCatalog.bak").exists())
        self.assertFalse((cat_dir / "ContentCatalog.txt").exists())
        stages = {e.stage for e in events}
        self.assertTrue({"download", "extract", "patch", "swap", "verify"} <= stages)
        self.assertGreater(result.downloaded_bytes, 0)
        # cached: a second download pass transfers nothing
        _, transferred = engine.download_all(plan, self.cache)
        self.assertEqual(transferred, 0)

    def test_restore_round_trip(self):
        self.index.raw["targets"]["1.6.1170"]["delete"] = ["Data/obsolete.bsa"]
        obsolete = self.game / "Data/obsolete.bsa"
        obsolete.write_bytes(b"obsolete")
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        engine.run(plan, cache_dir=self.cache, prefix_dir=self.prefix)
        self.assertFalse(obsolete.exists())
        manifest = json.loads((self.game / ".modsync-downgrade" / "manifest.json").read_text())
        self.assertEqual(manifest["from_version"], "1.7.104")
        self.assertEqual(manifest["originals"]["SkyrimSE.exe"]["sha1"], plan.from_exe_sha1)
        self.assertEqual(manifest["originals"]["Data/Skyrim.esm"]["size"], len(b"esm-new" * 1000))
        self.assertIn("Data/obsolete.bsa", manifest["removed"])

        events = []
        result = engine.restore(self.game, progress=events.append)
        self.assertEqual(result.from_version, "1.7.104")
        self.assertEqual(result.target, "1.6.1170")
        self.assertEqual(result.mismatches, [])
        self.assertEqual(
            sorted(result.restored),
            sorted([Path("SkyrimSE.exe"), Path("Data/Skyrim.esm"), Path("Data/obsolete.bsa")]),
        )
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertEqual((self.game / "Data" / "Skyrim.esm").read_bytes(), b"esm-new" * 1000)
        self.assertEqual(obsolete.read_bytes(), b"obsolete")
        self.assertFalse((self.game / ".modsync-downgrade").exists())
        self.assertFalse(engine.has_backup(self.game))
        self.assertEqual({e.stage for e in events}, {"restore"})
        # ...and the install is downgradable again
        engine.preflight(plan)
        self.assertEqual(engine.run(plan, cache_dir=self.cache).installed_version, "1.6.1170")

    def test_restore_reports_tampered_backup_without_aborting(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        engine.run(plan, cache_dir=self.cache, prefix_dir=self.prefix)
        backup = self.game / ".modsync-downgrade" / "backup"
        (backup / "SkyrimSE.exe").write_bytes(b"corrupted backup")
        (backup / "Data" / "Skyrim.esm").write_bytes(b"short")
        result = engine.restore(self.game)
        self.assertEqual(len(result.restored), 2)
        self.assertEqual(len(result.mismatches), 2)
        self.assertTrue(any("SkyrimSE.exe" in m and "SHA1" in m for m in result.mismatches))
        self.assertTrue(any("Skyrim.esm" in m and "bytes" in m for m in result.mismatches))
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), b"corrupted backup")
        self.assertFalse((self.game / ".modsync-downgrade").exists())

    def test_restore_without_backup(self):
        with self.assertRaisesRegex(engine.DowngradeError, "Verify integrity"):
            engine.restore(self.game)
        (self.game / ".modsync-downgrade" / "backup").mkdir(parents=True)
        with self.assertRaisesRegex(engine.DowngradeError, "nothing to restore"):
            engine.restore(self.game)
        with self.assertRaises(engine.DowngradeError):
            engine.discard_backup(self.game)

    def test_restore_after_failed_rollback(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        archives, _ = engine.download_all(plan, self.cache)
        replace = engine._replace

        def fail_swap_and_restore(src, dst):
            if src.name == "SkyrimSE.exe" and src.parent.name in ("out", "backup"):
                raise PermissionError("locked executable")
            replace(src, dst)

        with patch.object(engine, "_replace", side_effect=fail_swap_and_restore):
            with self.assertRaises(engine.SwapError):
                engine.apply(plan, archives)
        result = engine.restore(self.game)
        self.assertEqual(result.restored, [Path("SkyrimSE.exe")])
        self.assertEqual(result.mismatches, [])
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertEqual((self.game / "Data/Skyrim.esm").read_bytes(), b"esm-new" * 1000)
        self.assertFalse((self.game / ".modsync-downgrade").exists())

    def test_discard_backup(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        engine.run(plan, cache_dir=self.cache, prefix_dir=self.prefix)
        freed = engine.discard_backup(self.game)
        self.assertGreaterEqual(freed, len(self.new_exe))
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.old_exe)
        self.assertFalse((self.game / ".modsync-downgrade").exists())

    def test_preflight_rejects_wrong_source_version(self):
        (self.game / "SkyrimSE.exe").write_bytes(self.old_exe)
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        with self.assertRaises(engine.DowngradeError) as ctx:
            engine.preflight(plan)
        self.assertIn("1.6.1170", str(ctx.exception))
        self.assertIn("1.7.104", str(ctx.exception))

    def test_checksum_mismatch_deletes_download(self):
        bad = self.tmp / "bad.json"
        _write_index(bad, engine.sha1_of(self.game / "SkyrimSE.exe"), self.archive.as_uri(), "0" * 40)
        idx = recipe._parse(bad.read_text(), "test")
        plan = engine.make_plan(idx, self.game, "1.6.1170", "english")
        with self.assertRaises(engine.DowngradeError):
            engine.download_all(plan, self.cache)
        self.assertFalse(list(self.cache.rglob("*.7z")))

    def test_wrong_source_file_fails_before_swap(self):
        # Corrupt one source: xdelta3 rejects it, nothing must be replaced.
        (self.game / "Data" / "Skyrim.esm").write_bytes(b"tampered" * 1000)
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        archives, _ = engine.download_all(plan, self.cache)
        with self.assertRaises(tools.ToolError):
            engine.apply(plan, archives, prefix_dir=self.prefix)
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertFalse((self.game / ".modsync-downgrade").exists())

    def test_swap_failure_restores_originals_and_can_retry(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        archives, _ = engine.download_all(plan, self.cache)
        replace = engine._replace

        def fail_exe(src, dst):
            if src.parent.name == "out" and src.name == "SkyrimSE.exe":
                raise PermissionError("locked executable")
            replace(src, dst)

        with patch.object(engine, "_replace", side_effect=fail_exe):
            with self.assertRaisesRegex(engine.DowngradeError, "rolled back"):
                engine.apply(plan, archives)
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertEqual((self.game / "Data/Skyrim.esm").read_bytes(), b"esm-new" * 1000)
        self.assertFalse((self.game / ".modsync-downgrade").exists())
        self.assertEqual(engine.run(plan, cache_dir=self.cache).installed_version, "1.6.1170")

    def test_failed_rollback_preserves_backups_and_blocks_reuse(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        archives, _ = engine.download_all(plan, self.cache)
        replace = engine._replace

        def fail_swap_and_restore(src, dst):
            if src.name == "SkyrimSE.exe" and src.parent.name in ("out", "backup"):
                raise PermissionError("locked executable")
            replace(src, dst)

        with patch.object(engine, "_replace", side_effect=fail_swap_and_restore):
            with self.assertRaises(engine.SwapError) as ctx:
                engine.apply(plan, archives)
        backup = ctx.exception.backup_dir / "SkyrimSE.exe"
        self.assertEqual(backup.read_bytes(), self.new_exe)
        with self.assertRaisesRegex(engine.DowngradeError, "previous downgrade"):
            engine.apply(plan, archives)
        self.assertEqual(backup.read_bytes(), self.new_exe)

    def test_deletions_are_restored_on_interrupt_then_applied_on_retry(self):
        self.index.raw["targets"]["1.6.1170"]["delete"] = ["Data/obsolete.bsa", "another.dll"]
        obsolete = self.game / "Data/obsolete.bsa"
        another = self.game / "another.dll"
        obsolete.write_bytes(b"obsolete")
        another.write_bytes(b"another")
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        archives, _ = engine.download_all(plan, self.cache)

        def interrupt(progress):
            if progress.message == "Removing another.dll":
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            engine.apply(plan, archives, progress=interrupt)
        self.assertEqual(obsolete.read_bytes(), b"obsolete")
        self.assertEqual(another.read_bytes(), b"another")
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertEqual((self.game / "Data/Skyrim.esm").read_bytes(), b"esm-new" * 1000)
        result = engine.run(plan, cache_dir=self.cache)
        self.assertEqual(result.removed_files, [obsolete, another])
        self.assertFalse(obsolete.exists())
        self.assertFalse(another.exists())

    def test_wrong_staged_version_does_not_touch_install(self):
        plan = engine.make_plan(self.index, self.game, "1.6.1170", "english")
        plan.target = "1.5.97"
        archives, _ = engine.download_all(plan, self.cache)
        with self.assertRaisesRegex(engine.DowngradeError, "staged game"):
            engine.apply(plan, archives)
        self.assertEqual((self.game / "SkyrimSE.exe").read_bytes(), self.new_exe)
        self.assertEqual((self.game / "Data/Skyrim.esm").read_bytes(), b"esm-new" * 1000)


if __name__ == "__main__":
    unittest.main()
