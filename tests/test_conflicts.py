"""Conflict copies are found, described without guessing which side is
right, and settled by keeping one whole version and archiving the other."""

import tempfile
import unittest
from pathlib import Path

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

    def test_finds_conflicts_in_profiles_overwrite_and_the_root_only(self):
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "")
        self.write(f"overwrite/SKSE/Plugins/settings.{STAMP}.ini", "")
        self.write(f"categories.{STAMP}.dat", "")
        self.write(f"Makefile.{STAMP}", "")  # no extension
        self.write(f"mods/Big/huge.{STAMP}.esp", "")  # not searched
        self.write("profiles/Default/modlist.sync-conflict-notreally.txt", "")
        found = conflicts.find(self.inst)
        self.assertEqual([c.relative(self.inst) for c in found],
                         ["Makefile", "categories.dat", "overwrite/SKSE/Plugins/settings.ini",
                          "profiles/Default/modlist.txt"])
        modlist = found[-1]
        self.assertEqual(modlist.kind, "modlist")
        self.assertEqual(modlist.device, "ABCDEFG")
        self.assertEqual(modlist.when.isoformat(), "2026-10-04T10:15:00")
        self.assertEqual(found[2].kind, "file")

    def test_describes_a_modlist_difference_in_both_directions(self):
        self.write("profiles/Default/modlist.txt", "# MO2\n+Shared\n+OnlyHere\n-Toggled\n+Both\n*DLC: Dawnguard\n")
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Both\n+Toggled\n+Shared\n+OnlyThere\n+OnlyThere\n")
        (c,) = conflicts.find(self.inst)
        lines = conflicts.differences(c, "Steam Deck")
        self.assertIn("Only in the version from Steam Deck: OnlyThere.", lines)
        self.assertIn("Only in the version in use: OnlyHere.", lines)
        self.assertIn("Enabled only in the version from Steam Deck: Toggled.", lines)
        self.assertIn("The mods in both are in a different order.", lines)
        self.assertFalse(any("DLC" in line for line in lines))

    def test_plugins_compare_case_insensitively(self):
        self.write("profiles/Default/plugins.txt", "*Skyrim.esm\n*A.esp\nB.esp\n")
        self.write(f"profiles/Default/plugins.{STAMP}.txt", "*skyrim.esm\n*A.esp\nB.esp\n")
        (c,) = conflicts.find(self.inst)
        self.assertEqual(conflicts.differences(c), ["Both versions list the same plugins the same way."])

    def test_other_files_compare_bytes(self):
        self.write("overwrite/x.ini", "a=1")
        self.write(f"overwrite/x.{STAMP}.ini", "a=22")
        (c,) = conflicts.find(self.inst)
        self.assertIn("contents differ", conflicts.differences(c)[0])

    def test_keeping_the_current_version_archives_the_other(self):
        self.write("profiles/Default/modlist.txt", "+Mine\n")
        other = self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Theirs\n")
        (c,) = conflicts.find(self.inst)
        moved = conflicts.resolve(self.inst, c, "current", stamp="t")
        self.assertFalse(other.exists())
        self.assertEqual(moved, self.inst / ".modsync-conflicts" / "t" / "profiles" / "Default" / other.name)
        self.assertEqual(moved.read_text(), "+Theirs\n")
        self.assertEqual((self.profile / "modlist.txt").read_text(), "+Mine\n")
        self.assertEqual(conflicts.find(self.inst), [])

    def test_using_the_other_version_archives_the_current(self):
        self.write("profiles/Default/modlist.txt", "+Mine\n")
        self.write(f"profiles/Default/modlist.{STAMP}.txt", "+Theirs\n")
        (c,) = conflicts.find(self.inst)
        moved = conflicts.resolve(self.inst, c, "other", stamp="t")
        self.assertEqual((self.profile / "modlist.txt").read_text(), "+Theirs\n")
        self.assertEqual(moved.read_text(), "+Mine\n")
        self.assertEqual(conflicts.find(self.inst), [])
        with self.assertRaises(ValueError):
            conflicts.resolve(self.inst, c, "both")

    def test_archives_stay_out_of_sync(self):
        # Both archives sit at the instance root, which .stignore's "/*" excludes.
        patterns = stignore.PATTERNS
        self.assertIn("/*", patterns)
        for name in (conflicts.ARCHIVE_DIR, ".modsync-before-join"):
            self.assertFalse(any(p.startswith("!") and name in p for p in patterns))
