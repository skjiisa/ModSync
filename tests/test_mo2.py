import tempfile
import unittest
from pathlib import Path

from modsync.mo2 import discover
from modsync.mo2 import ini as ini_mod
from modsync.mo2 import instance as inst

# A realistic Linux/Proton portable ModOrganizer.ini. Raw string => the doubled
# backslashes are literal, exactly as MO2 writes them inside @ByteArray(...).
INI = r"""[General]
gameName=@ByteArray(Skyrim Special Edition)
gamePath=@ByteArray(Z:\\home\\deck\\.local\\share\\Steam\\steamapps\\common\\Skyrim Special Edition)
selected_profile=@ByteArray(Default)
version=2.5.2

[Settings]
download_directory=%BASE_DIR%/downloads
mod_directory=%BASE_DIR%/mods
"""


class IniHelperTests(unittest.TestCase):
    def test_unwrap_bytearray(self):
        self.assertEqual(
            ini_mod.unwrap_bytearray("@ByteArray(Skyrim Special Edition)"),
            "Skyrim Special Edition",
        )
        self.assertEqual(ini_mod.unwrap_bytearray(r"@ByteArray(Z:\\home\\x)"), r"Z:\home\x")

    def test_wine_to_local(self):
        self.assertEqual(str(ini_mod.wine_to_local(r"Z:\home\deck\x")), "/home/deck/x")
        self.assertIsNone(ini_mod.wine_to_local(r"C:\Program Files"))
        self.assertEqual(str(ini_mod.wine_to_local("/already/posix")), "/already/posix")

    def test_expand_tokens(self):
        self.assertEqual(ini_mod.expand_tokens("%BASE_DIR%/mods", "/inst"), "/inst/mods")
        self.assertEqual(ini_mod.expand_tokens("%DOCUMENTS%/x", "/inst"), "%DOCUMENTS%/x")


class InstanceInspectTests(unittest.TestCase):
    def _make_instance(self, tmp: str) -> Path:
        instdir = Path(tmp) / "MO2_SkyrimSE"
        (instdir / "mods" / "SomeMod").mkdir(parents=True)
        (instdir / "downloads").mkdir()
        (instdir / "profiles" / "Default").mkdir(parents=True)
        (instdir / "overwrite").mkdir()
        (instdir / "ModOrganizer.ini").write_text(INI)
        (instdir / "ModOrganizer.exe").write_bytes(b"MZ")
        return instdir

    def test_inspect_full_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            instdir = self._make_instance(tmp)
            info = inst.inspect(instdir)
            self.assertTrue(info.has_ini)
            self.assertEqual(info.game_name, "Skyrim Special Edition")
            self.assertEqual(info.selected_profile, "Default")
            self.assertEqual(
                str(info.game_path_local),
                "/home/deck/.local/share/Steam/steamapps/common/Skyrim Special Edition",
            )
            for name in ("mods", "downloads", "profiles", "overwrite"):
                self.assertTrue(
                    info.content_dirs[name].inside_instance,
                    f"{name} should be inside the instance",
                )
            self.assertIn("Default", info.profiles)
            self.assertEqual(info.issues, [])

    def test_discover_finds_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            instdir = self._make_instance(tmp)
            found = discover.discover_instances([Path(tmp)], [])
            self.assertIn(instdir.resolve(), found)



class BrokenInstanceTests(unittest.TestCase):
    def test_ini_without_content_dirs_or_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            instdir = Path(tmp) / "MO2"
            instdir.mkdir()
            (instdir / "ModOrganizer.ini").write_text(INI)
            info = inst.inspect(instdir)
            self.assertTrue(info.has_ini)
            self.assertEqual(info.profiles, [])
            for name in ("mods", "downloads", "profiles", "overwrite"):
                self.assertFalse(info.content_dirs[name].exists, name)
                self.assertTrue(info.content_dirs[name].inside_instance, name)
            self.assertEqual(info.issues, [])
            self.assertFalse(discover.is_instance(instdir))  # needs mods/ to count

    def test_garbage_ini_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            instdir = Path(tmp) / "MO2"
            (instdir / "mods").mkdir(parents=True)
            (instdir / "ModOrganizer.ini").write_bytes(b"\x00\xff\xfe[General\n=\nkey\n")
            info = inst.inspect(instdir)
            self.assertFalse(info.has_ini)
            self.assertEqual(info.game_name, "")
            self.assertIsNone(info.game_path_local)
            self.assertIn("No readable ModOrganizer.ini found.", info.issues)
            self.assertTrue(discover.is_instance(instdir))

    def test_missing_ini_and_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            instdir = Path(tmp) / "MO2"
            (instdir / "mods").mkdir(parents=True)
            info = inst.inspect(instdir)
            self.assertFalse(info.has_ini)
            self.assertIn("No readable ModOrganizer.ini found.", info.issues)
            self.assertFalse(discover.is_instance(instdir))
            self.assertEqual(discover.discover_instances([Path(tmp)], []), [])

if __name__ == "__main__":
    unittest.main()
