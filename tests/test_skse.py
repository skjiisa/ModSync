"""SKSE install: the right build for the installed runtime, old files out,
new files in — with the download and the 7z extractor faked."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import skse
from modsync.downgrade import engine, tools
from modsync.gameversion import GameVersion
from modsync.service import GameStatus

from tests.test_game_status import status


class BuildTable(unittest.TestCase):
    def test_known_runtimes(self):
        self.assertEqual(skse.build_for("1.5.97").version, "2.0.20")
        self.assertEqual(skse.build_for(GameVersion.parse("1.6.1170")).version, "2.2.6")
        self.assertEqual(skse.build_for("1.6.1170").dll_name, "skse64_1_6_1170.dll")
        self.assertIsNone(skse.build_for("1.7.99"))
        self.assertIsNone(skse.build_for(None))

    def test_nexus_only_build_has_a_page_but_no_download(self):
        b = skse.build_for("1.7.104")
        self.assertFalse(b.downloadable)
        self.assertIn("nexusmods.com", b.page)

    def test_downloadable_builds_are_fully_specified(self):
        for b in skse.BUILDS:
            if b.downloadable:
                self.assertTrue(b.url.startswith("https://skse.silverlock.org/"), b)
                self.assertEqual(len(b.sha256), 64, b)
                self.assertTrue(b.subdir, b)


class GameStatusSkse(unittest.TestCase):
    def test_states(self):
        self.assertEqual(status("1.6.1170", "1.6.1170", "1.6.1170").skse_state, "ok")
        self.assertEqual(status("1.6.1170", "1.6.1170", "1.5.97").skse_state, "wrong")
        self.assertEqual(status("1.6.1170", "1.6.1170", None).skse_state, "missing")
        self.assertEqual(status("1.6.1170", None, None, skse_runtimes=["1.5.97", "1.6.1170"]).skse_state, "several")
        leftovers = status("1.6.1170", "1.6.1170", "1.6.1170", skse_case_duplicates=62)
        self.assertEqual(leftovers.skse_state, "several")
        self.assertEqual(leftovers.skse_build.version, "2.2.6")  # reinstalling cleans them up

    def test_no_skse_advice_until_the_game_version_itself_is_right(self):
        self.assertEqual(status("1.7.104", "1.6.1170", "1.5.97").skse_state, "")  # needs downgrade first
        self.assertEqual(status(None, "1.6.1170", None).skse_state, "")
        self.assertEqual(status("1.6.1170", "1.6.1170", "1.5.97", steam_updating=True).skse_state, "")

    def test_build_only_when_needed_and_known(self):
        self.assertEqual(status("1.6.1170", "1.6.1170", "1.5.97").skse_build.version, "2.2.6")
        self.assertIsNone(status("1.6.1170", "1.6.1170", "1.6.1170").skse_build)
        self.assertIsNone(status("1.7.99", "1.7.99", None).skse_build)  # unknown runtime
        self.assertFalse(status("1.7.104", None, None).skse_build.downloadable)


class Install(unittest.TestCase):
    """The archive is a stand-in file whose sha256 the build carries; the
    extractor is faked to lay out a prepared tree."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.game = self.root / "Skyrim Special Edition"
        self.game.mkdir()
        (self.game / "SkyrimSE.exe").write_bytes(b"exe")
        (self.game / "skse64_1_5_97.dll").write_bytes(b"old")
        (self.game / "skse64_loader.exe").write_bytes(b"old loader")
        (self.game / "skse64_steam_loader.dll").write_bytes(b"old steam loader")
        (self.game / "Data" / "Scripts").mkdir(parents=True)
        (self.game / "Data" / "Scripts" / "actor.pex").write_bytes(b"old pex")
        (self.game / "Data" / "Skyrim.esm").write_bytes(b"keep")
        self.cache = self.root / "cache"

        self.payload = b"pretend 7z"
        self.build = skse.SkseBuild(
            "2.2.6", "1.6.1170", "https://example.invalid/skse64_2_02_06.7z",
            hashlib.sha256(self.payload).hexdigest(), "skse64_2_02_06",
        )

    def _fake_download(self, url, dest, progress, label, sha1=None):
        dest.write_bytes(self.payload)
        if progress:
            progress(engine.Progress("download", label, 10, 10))
        return len(self.payload)

    def _fake_extractor(self, layout):
        class Fake:
            name = "fake"

            @staticmethod
            def extract(archive, dest):
                for rel, data in layout.items():
                    p = Path(dest) / rel
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)

        return patch.object(tools, "find_extractor", return_value=Fake()), Fake()

    GOOD = {
        "skse64_2_02_06/skse64_1_6_1170.dll": b"new dll",
        "skse64_2_02_06/skse64_loader.exe": b"new loader",
        "skse64_2_02_06/skse64_readme.txt": b"not installed",
        "skse64_2_02_06/src/skse64/main.cpp": b"not installed",
        "skse64_2_02_06/Data/Scripts/actor.pex": b"new pex",
        "skse64_2_02_06/Data/Scripts/Source/actor.psc": b"source",
    }

    def test_replaces_old_skse_and_leaves_the_rest_of_the_game_alone(self):
        p, _ = self._fake_extractor(self.GOOD)
        seen = []
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            result = skse.install(self.build, self.game, self.cache, seen.append)
        names = sorted(f.name for f in self.game.iterdir())
        self.assertEqual(names, ["Data", "SkyrimSE.exe", "skse64_1_6_1170.dll", "skse64_loader.exe"])
        self.assertEqual((self.game / "skse64_1_6_1170.dll").read_bytes(), b"new dll")
        self.assertEqual((self.game / "Data" / "Scripts" / "actor.pex").read_bytes(), b"new pex")
        self.assertEqual((self.game / "Data" / "Skyrim.esm").read_bytes(), b"keep")
        self.assertTrue((self.game / "Data" / "Scripts" / "Source" / "actor.psc").exists())
        self.assertEqual(sorted(result.removed), ["skse64_1_5_97.dll", "skse64_steam_loader.dll"])
        self.assertEqual(result.files, 4)
        self.assertEqual([s.stage for s in seen], ["download", "extract", "install"])
        self.assertTrue((self.cache / "skse64_2_02_06.7z").exists())  # kept for next time

    def test_another_builds_scripts_in_other_case_are_replaced_not_kept(self):
        """2.0.20 names scripts in CamelCase and 2.2.6 in lowercase. Linux kept
        both; Wine loads either, and Steam can't move the game."""
        scripts = self.game / "Data" / "Scripts"
        (scripts / "actor.pex").unlink()
        (scripts / "Actor.pex").write_bytes(b"2.0.20 pex")
        (scripts / "Source").mkdir()
        (scripts / "Source" / "Actor.psc").write_bytes(b"2.0.20 source")
        (scripts / "MyMod.pex").write_bytes(b"not SKSE's")
        self.assertEqual(skse.case_duplicates(self.game), [])  # one spelling each so far
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            result = skse.install(self.build, self.game, self.cache)
        self.assertEqual(sorted(f.name for f in scripts.iterdir()), ["MyMod.pex", "Source", "actor.pex"])
        self.assertEqual((scripts / "actor.pex").read_bytes(), b"new pex")
        self.assertEqual([f.name for f in (scripts / "Source").iterdir()], ["actor.psc"])
        self.assertEqual(result.old_scripts, 2)
        self.assertEqual(sorted(result.removed), ["skse64_1_5_97.dll", "skse64_steam_loader.dll"])

    def test_an_install_that_already_has_both_spellings_is_found_and_cleaned(self):
        scripts = self.game / "Data" / "Scripts"
        (scripts / "Actor.pex").write_bytes(b"2.0.20 pex")  # beside actor.pex, as an older ModSync left it
        dupes = skse.case_duplicates(self.game)
        self.assertEqual(dupes, [[scripts / "Actor.pex", scripts / "actor.pex"]])
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            skse.install(self.build, self.game, self.cache)
        self.assertEqual(skse.case_duplicates(self.game), [])
        self.assertEqual((scripts / "actor.pex").read_bytes(), b"new pex")

    def test_folders_keep_the_spelling_already_on_disk(self):
        (self.game / "Data").rename(self.game / "data")
        (self.game / "data" / "Scripts").rename(self.game / "data" / "scripts")
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            skse.install(self.build, self.game, self.cache)
        self.assertEqual(sorted(f.name for f in self.game.iterdir()),
                         ["SkyrimSE.exe", "data", "skse64_1_6_1170.dll", "skse64_loader.exe"])
        self.assertEqual((self.game / "data" / "scripts" / "actor.pex").read_bytes(), b"new pex")
        self.assertTrue((self.game / "data" / "scripts" / "Source" / "actor.psc").exists())

    def test_nothing_is_deleted_through_a_link_to_outside_the_game_folder(self):
        outside = self.root / "elsewhere"  # say, a Data folder kept on another drive
        (outside / "Scripts").mkdir(parents=True)
        (outside / "Scripts" / "Actor.pex").write_bytes(b"not ours to delete")
        for p in (self.game / "Data").rglob("*"):
            if p.is_file():
                p.unlink()
        (self.game / "Data" / "Scripts").rmdir()
        (self.game / "Data").rmdir()
        (self.game / "data").symlink_to(outside)
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            result = skse.install(self.build, self.game, self.cache)
        self.assertEqual((outside / "Scripts" / "Actor.pex").read_bytes(), b"not ours to delete")
        self.assertEqual(result.old_scripts, 0)

    def test_a_linked_script_is_left_alone(self):
        scripts = self.game / "Data" / "Scripts"
        (self.root / "shared.pex").write_bytes(b"shared")
        (scripts / "Actor.pex").symlink_to(self.root / "shared.pex")
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            skse.install(self.build, self.game, self.cache)
        self.assertTrue((scripts / "Actor.pex").is_symlink())
        self.assertEqual((self.root / "shared.pex").read_bytes(), b"shared")

    def test_every_spelling_of_a_folder_is_cleaned_and_checked(self):
        other = self.game / "Data" / "scripts"  # beside Data/Scripts
        other.mkdir()
        (other / "Actor.pex").write_bytes(b"2.0.20 pex")
        (other / "MyMod.pex").write_bytes(b"a mod's")
        self.assertEqual(skse.case_duplicates(self.game),
                         [sorted([other / "Actor.pex", self.game / "Data" / "Scripts" / "actor.pex"])])
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            result = skse.install(self.build, self.game, self.cache)
        self.assertEqual(result.old_scripts, 1)
        self.assertEqual([f.name for f in other.iterdir()], ["MyMod.pex"])
        self.assertEqual(skse.case_duplicates(self.game), [])

    def test_only_scripts_skse_ships_count(self):
        scripts = self.game / "Data" / "Scripts"
        (scripts / "MyQuest.pex").write_bytes(b"a")
        (scripts / "myquest.pex").write_bytes(b"b")  # a mod's own problem; reinstalling SKSE can't help
        self.assertEqual(skse.case_duplicates(self.game), [])

    def test_cached_archive_is_reused(self):
        self.cache.mkdir()
        (self.cache / "skse64_2_02_06.7z").write_bytes(self.payload)
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download") as dl:
            skse.install(self.build, self.game, self.cache)
        dl.assert_not_called()

    def test_checksum_mismatch_deletes_the_download(self):
        bad = skse.SkseBuild("2.2.6", "1.6.1170", self.build.url, "00" * 32, "skse64_2_02_06")
        p, _ = self._fake_extractor(self.GOOD)
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            with self.assertRaises(skse.SkseError):
                skse.install(bad, self.game, self.cache)
        self.assertFalse((self.cache / "skse64_2_02_06.7z").exists())
        self.assertTrue((self.game / "skse64_1_5_97.dll").exists())  # nothing touched

    def test_archive_without_the_expected_files_changes_nothing(self):
        p, _ = self._fake_extractor({"skse64_2_02_06/readme.txt": b"?"})
        with p, patch.object(engine, "_download", side_effect=self._fake_download):
            with self.assertRaises(skse.SkseError):
                skse.install(self.build, self.game, self.cache)
        self.assertTrue((self.game / "skse64_1_5_97.dll").exists())

    def test_refuses_a_folder_that_is_not_the_game_and_a_nexus_only_build(self):
        with self.assertRaises(skse.SkseError):
            skse.install(self.build, self.root, self.cache)
        with self.assertRaises(skse.SkseError) as ctx:
            skse.install(skse.build_for("1.7.104"), self.game, self.cache)
        self.assertIn("nexusmods", str(ctx.exception))

    def test_no_extractor(self):
        with patch.object(tools, "find_extractor", return_value=None), \
                patch.object(engine, "_download", side_effect=self._fake_download):
            with self.assertRaises(skse.SkseError) as ctx:
                skse.install(self.build, self.game, self.cache)
        self.assertIn("7z", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
