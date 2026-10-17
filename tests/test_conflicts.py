"""Conflict copies are found, described without guessing which side is
right, and settled by keeping one whole version and archiving the other."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync.sync import conflicts, stignore

STAMP = "sync-conflict-20261004-101500-ABCDEFG"


class ConflictTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.inst = Path(tmp.name)
        self.profile = self.inst / "profiles" / "Default"
        self.profile.mkdir(parents=True)

    def write(self, rel, text):
        path = self.inst / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_finds_conflicts_in_every_synced_folder_and_the_root(self):
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "")
        self.write(f"overwrite/SKSE/Plugins/settings.{STAMP}.ini", "")
        self.write(f"categories.{STAMP}.dat", "")
        self.write(f"Makefile.{STAMP}", "")  # no extension
        self.write(f"mods/Big/SKSE/Plugins/big.{STAMP}.ini", "")
        self.write(f"downloads/Big-123.7z.{STAMP}.meta", "")
        self.write(f".modsync-conflicts/20261004-101500/mods/Old/old.{STAMP}.ini", "")  # an archive: not synced
        self.write(f"logs/mo_interface.{STAMP}.log", "")  # not synced either
        self.write("profiles/Default/modlist.sync-conflict-notreally.txt", "")
        found = conflicts.find(self.inst)
        self.assertEqual([c.relative(self.inst) for c in found],
                         ["profiles/Default/modlist.txt", "Makefile", "categories.dat", "downloads/Big-123.7z.meta",
                          "mods/Big/SKSE/Plugins/big.ini", "overwrite/SKSE/Plugins/settings.ini"])
        modlist = found[0]
        self.assertEqual(modlist.kind, "modlist")
        self.assertEqual(modlist.device, "ABCDEFG")
        self.assertEqual(modlist.when.isoformat(), "2026-10-04T10:15:00")
        self.assertEqual(found[4].kind, "file")

    def test_describes_a_modlist_difference_in_both_directions(self):
        self.write("profiles/Default/modlist.txt", "# MO2\n+Shared\n+OnlyHere\n-Toggled\n+Both\n*DLC: Dawnguard\n")
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Both\n+Toggled\n+Shared\n+OnlyThere\n+OnlyThere\n")
        (c,) = conflicts.find(self.inst)
        lines = conflicts.differences(c)
        self.assertIn("Only in the other version: OnlyThere.", lines)
        self.assertIn("Only in the version in use: OnlyHere.", lines)
        self.assertIn("Enabled only in the other version: Toggled.", lines)
        self.assertIn("The mods in both are in a different order.", lines)
        self.assertFalse(any("DLC" in line for line in lines))

    def test_plugins_compare_case_insensitively(self):
        self.write("profiles/Default/plugins.txt", "*Skyrim.esm\n*A.esp\nB.esp\n")
        self.write(f"profiles/Default/plugins.{STAMP}.txt", "*skyrim.esm\n*A.esp\nB.esp\n")
        (c,) = conflicts.find(self.inst)
        self.assertEqual(conflicts.differences(c), ["Both versions list the same plugins the same way."])

    def test_other_files_compare_bytes(self):
        self.write("overwrite/x.json", "{}")
        self.write(f"overwrite/x.{STAMP}.json", "{ }")
        (c,) = conflicts.find(self.inst)
        self.assertEqual(conflicts.differences(c), ["The contents differ (2 bytes in use, 3 in the other)."])

    def test_describes_an_ini_difference_by_setting(self):
        self.write("mods/Tweaks/SKSE/Plugins/tweaks.ini",
                   "; tweaks\n[General]\nbFast = 1\nfScale=1.0\n[Extra]\niMine=3\n")
        self.write(f"mods/Tweaks/SKSE/Plugins/tweaks.{STAMP}.ini",
                   "[general]\nBFAST=1\nfScale = 2.5\nsNew=\n")
        (c,) = conflicts.find(self.inst)
        self.assertEqual(conflicts.differences(c), [
            "Settings only in the other version: [general] sNew.",
            "Settings only in the version in use: [Extra] iMine.",
            "Set differently: [General] fScale (1.0 in use, 2.5 in the other).",
        ])

    def test_download_meta_files_read_as_ini(self):
        self.write("downloads/Mod-1.7z.meta", "[General]\nversion=1.0\ninstalled=true\n")
        self.write(f"downloads/Mod-1.7z.{STAMP}.meta", "[General]\ninstalled=true\nversion=1.0\n")
        (c,) = conflicts.find(self.inst)
        self.assertEqual(conflicts.differences(c), ["Both versions have the same settings, written differently."])

    def test_an_ini_that_does_not_parse_falls_back_to_bytes(self):
        self.write("mods/Odd/odd.ini", "not settings at all\n")
        self.write(f"mods/Odd/odd.{STAMP}.ini", "[x]\na=1\n")
        (c,) = conflicts.find(self.inst)
        self.assertIn("The contents differ", conflicts.differences(c)[0])

    def test_keeping_the_current_version_archives_the_other(self):
        self.write("profiles/Default/modlist.txt", "+Mine\n")
        other = self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Theirs\n")
        (c,) = conflicts.find(self.inst)
        moved = conflicts.resolve(self.inst, c, "current")
        self.assertFalse(other.exists())
        self.assertEqual(moved.relative_to(self.inst).parts[0], ".modsync-conflicts")
        self.assertEqual(moved.relative_to(self.inst).parts[2:], ("profiles", "Default", other.name))
        self.assertEqual(moved.read_text(), "+Theirs\n")
        self.assertEqual((self.profile / "modlist.txt").read_text(), "+Mine\n")
        self.assertEqual(conflicts.find(self.inst), [])

    def test_using_the_other_version_archives_the_current(self):
        self.write("profiles/Default/modlist.txt", "+Mine\n")
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Theirs\n")
        (c,) = conflicts.find(self.inst)
        moved = conflicts.resolve(self.inst, c, "other")
        self.assertEqual((self.profile / "modlist.txt").read_text(), "+Theirs\n")
        self.assertEqual(moved.read_text(), "+Mine\n")
        self.assertEqual(conflicts.find(self.inst), [])
        with self.assertRaises(ValueError):
            conflicts.resolve(self.inst, c, "both")

    def test_two_resolutions_in_the_same_second_keep_both_displaced_versions(self):
        for n in (1, 2):
            self.write("profiles/Default/modlist.txt", f"+Version{n}\n")
            self.write(f"profiles/Default/modlist.{STAMP}.txt", f"+Other{n}\n")
            (c,) = conflicts.find(self.inst)
            conflicts.resolve(self.inst, c, "other")
        kept = sorted(p.read_text() for p in (self.inst / ".modsync-conflicts").rglob("modlist.txt"))
        self.assertEqual(kept, ["+Version1\n", "+Version2\n"])
        archive = conflicts.new_archive(self.inst)
        (archive / "x").write_text("")
        self.assertNotEqual(conflicts.new_archive(self.inst), archive)

    def test_a_profiles_lists_are_decided_together(self):
        other_device = STAMP.replace("AAAAAAA", "BBBBBBB").replace("ABCDEFG", "BBBBBBB")
        for name in ("modlist.txt", "plugins.txt", "loadorder.txt", "settings.ini"):
            self.write(f"profiles/Default/{name}", "")
            stem, ext = name.rsplit(".", 1)
            self.write(f"profiles/Default/{stem}.{STAMP}.{ext}", "")
        self.write(f"profiles/Default/modlist.{other_device}.txt", "")  # a second copy, another machine
        self.write("profiles/Other/modlist.txt", "")
        self.write(f"profiles/Other/modlist.{STAMP}.txt", "")
        found = conflicts.find(self.inst)
        first = next(c for c in found if c.kind == "loadorder")
        group = conflicts.related(found, first)
        self.assertEqual(sorted(c.original.name for c in group), ["loadorder.txt", "modlist.txt", "plugins.txt"])
        self.assertTrue(all(c.device == "ABCDEFG" for c in group))
        ini = next(c for c in found if c.original.name == "settings.ini")
        self.assertEqual(conflicts.related(found, ini), [ini])

    def test_the_chosen_copy_leads_its_profile_and_companions_are_the_nearest_in_time(self):
        first, second, later = ("sync-conflict-20261001-120000-ABCDEFG", "sync-conflict-20261002-120000-ABCDEFG",
                                "sync-conflict-20261002-120300-ABCDEFG")
        self.write("profiles/Default/modlist.txt", "+Now\n")
        self.write("profiles/Default/plugins.txt", "*Now.esp\n")
        self.write(f"profiles/Default/modlist.{first}.txt", "+October1\n")
        self.write(f"profiles/Default/modlist.{second}.txt", "+October2\n")
        self.write(f"profiles/Default/plugins.{later}.txt", "*October2.esp\n")
        found = conflicts.find(self.inst)
        oct1, oct2 = (next(c for c in found if c.path.name == f"modlist.{s}.txt") for s in (first, second))
        group = conflicts.related(found, oct2)
        self.assertEqual([c.path.name for c in group], [f"modlist.{second}.txt", f"plugins.{later}.txt"])
        self.assertEqual(conflicts.related(found, oct1), [oct1])  # a day away from the plugins copy

    def test_using_a_copy_that_has_gone_changes_nothing(self):
        live = self.write("profiles/Default/modlist.txt", "+Mine\n")
        other = self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Theirs\n")
        (c,) = conflicts.find(self.inst)
        other.unlink()  # another machine settled it meanwhile
        for keep in ("other", "current"):
            with self.assertRaises(conflicts.GoneError):
                conflicts.resolve(self.inst, c, keep)
        self.assertEqual(live.read_text(), "+Mine\n")
        self.assertFalse((self.inst / ".modsync-conflicts").exists())

    def test_undo_puts_both_versions_back(self):
        live = self.write("profiles/Default/modlist.txt", "+Mine\n")
        other = self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Theirs\n")
        for keep in ("other", "current"):
            (c,) = conflicts.find(self.inst)
            moved = conflicts.resolve(self.inst, c, keep)
            conflicts.undo(self.inst, c, keep, moved)
            self.assertEqual((live.read_text(), other.read_text()), ("+Mine\n", "+Theirs\n"))

    def test_large_identical_files_are_compared_without_reading_them_whole(self):
        data = os.urandom(1 << 20)
        self.write("downloads/Big.7z", "")
        (self.inst / "downloads" / "Big.7z").write_bytes(data)
        (self.inst / "downloads" / f"Big.{STAMP}.7z").write_bytes(data)
        (c,) = conflicts.find(self.inst)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("read whole")):
            self.assertEqual(conflicts.differences(c), ["Both versions are identical."])

    def test_archives_stay_out_of_sync(self):
        # Both archives sit at the instance root, which .stignore's "/*" excludes.
        patterns = stignore.PATTERNS
        self.assertIn("/*", patterns)
        for name in (conflicts.ARCHIVE_DIR, ".modsync-before-join"):
            self.assertFalse(any(p.startswith("!") and name in p for p in patterns))
