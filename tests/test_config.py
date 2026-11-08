"""Where ModSync keeps its settings and data: one place for every copy on a machine."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import config

# The real ones, before tests/conftest.py stubs them out for every test.
_FLATPAK_INSTALLED = config.flatpak_installed
_FLATPAK_HOME = config.flatpak_home


class Locations(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = {k: v for k, v in os.environ.items()
               if k not in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "MODSYNC_HOME", "FLATPAK_ID")}
        env["HOME"] = str(self.home)
        for p in (patch.dict(os.environ, env, clear=True),
                  patch.object(config, "flatpak_home", _FLATPAK_HOME),
                  patch.object(config, "flatpak_installed", _FLATPAK_INSTALLED),
                  patch.object(config, "SYSTEM_FLATPAK", self.home / "system-flatpak"),
                  patch.object(config.Path, "exists", self._exists_except_flatpak_info)):
            p.start()
            self.addCleanup(p.stop)
        self.var_app = self.home / ".var" / "app" / "io.github.skjiisa.ModSync"

    @staticmethod
    def _exists_except_flatpak_info(path, _real=Path.exists):
        return False if str(path) == "/.flatpak-info" else _real(path)

    def where(self) -> tuple[Path, Path]:
        return config.config_dir(), config.data_dir()

    def install_flatpak(self):
        (self.home / ".local" / "share" / "flatpak" / "app" / "io.github.skjiisa.ModSync").mkdir(parents=True)

    def test_without_the_flatpak_the_standard_folders(self):
        self.assertEqual(self.where(), (self.home / ".config/modsync", self.home / ".local/share/modsync"))

    def test_with_the_flatpak_installed_its_folders(self):
        self.install_flatpak()
        self.assertTrue(config.flatpak_installed())
        self.assertEqual(self.where(), (self.var_app / "config/modsync", self.var_app / "data/modsync"))

    def test_a_system_wide_flatpak_counts(self):
        (self.home / "system-flatpak" / "app" / "io.github.skjiisa.ModSync").mkdir(parents=True)
        self.assertTrue(config.flatpak_installed())
        self.assertEqual(self.where(), (self.var_app / "config/modsync", self.var_app / "data/modsync"))

    def test_the_flatpaks_data_left_behind_is_still_used(self):
        (self.var_app / "config" / "modsync").mkdir(parents=True)
        self.assertFalse(config.flatpak_installed())
        self.assertEqual(config.config_dir(), self.var_app / "config/modsync")

    def test_inside_the_flatpak_its_own_xdg_folders(self):
        self.install_flatpak()
        os.environ.update(FLATPAK_ID="io.github.skjiisa.ModSync",
                          XDG_CONFIG_HOME=str(self.var_app / "config"), XDG_DATA_HOME=str(self.var_app / "data"))
        self.assertEqual(self.where(), (self.var_app / "config/modsync", self.var_app / "data/modsync"))

    def test_xdg_set_to_the_standard_place_still_shares_the_flatpaks(self):
        self.install_flatpak()
        os.environ.update(XDG_CONFIG_HOME=str(self.home / ".config"), XDG_DATA_HOME=str(self.home / ".local/share"))
        self.assertEqual(self.where(), (self.var_app / "config/modsync", self.var_app / "data/modsync"))

    def test_xdg_set_elsewhere_wins(self):
        self.install_flatpak()
        os.environ.update(XDG_CONFIG_HOME=str(self.home / "cfg"), XDG_DATA_HOME=str(self.home / "dat"))
        self.assertEqual(self.where(), (self.home / "cfg/modsync", self.home / "dat/modsync"))

    def test_modsync_home_wins_over_everything(self):
        self.install_flatpak()
        os.environ.update(MODSYNC_HOME="~/modsync-dev", XDG_CONFIG_HOME=str(self.home / "cfg"))
        self.assertEqual(self.where(), (self.home / "modsync-dev/config", self.home / "modsync-dev/data"))

    def write_state(self, folder: Path) -> Path:
        folder.mkdir(parents=True)
        (folder / "state.json").write_text("{}")
        return folder

    def test_a_copy_with_its_own_setup_keeps_it_when_the_flatpak_is_installed(self):
        own = self.write_state(self.home / ".config" / "modsync")
        self.install_flatpak()
        self.assertEqual(self.where(), (own, self.home / ".local/share/modsync"))
        self.assertIsNone(config.other_setup())  # the Flatpak has none yet

    def test_two_setups_are_pointed_out_from_either_copy(self):
        own = self.write_state(self.home / ".config" / "modsync")
        flatpaks = self.write_state(self.var_app / "config" / "modsync")
        self.assertEqual(config.config_dir(), own)  # never switched to the Flatpak's old one
        self.assertEqual(config.other_setup(), flatpaks)
        os.environ.update(FLATPAK_ID="io.github.skjiisa.ModSync", XDG_CONFIG_HOME=str(self.var_app / "config"))
        self.assertEqual(config.other_setup(), own)

    def test_a_shared_setup_has_no_other(self):
        self.install_flatpak()
        self.write_state(self.var_app / "config" / "modsync")
        self.assertIsNone(config.other_setup())

if __name__ == "__main__":
    unittest.main()
