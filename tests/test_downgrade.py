import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

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


def _write_index(path: Path, from_sha1: str, url: str, sha1: str) -> None:
    doc = {
        "schema": 1,
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
        self.assertFalse((self.game / ".modsync-downgrade").exists())
        cat_dir = self.prefix / "drive_c/users/steamuser/AppData/Local/Skyrim Special Edition"
        self.assertTrue((cat_dir / "ContentCatalog.bak").exists())
        self.assertFalse((cat_dir / "ContentCatalog.txt").exists())
        stages = {e.stage for e in events}
        self.assertTrue({"download", "extract", "patch", "swap", "verify"} <= stages)
        self.assertGreater(result.downloaded_bytes, 0)
        # cached: a second download pass transfers nothing
        _, transferred = engine.download_all(plan, self.cache)
        self.assertEqual(transferred, 0)

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


if __name__ == "__main__":
    unittest.main()
