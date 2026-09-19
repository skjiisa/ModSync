import tempfile
import unittest
from pathlib import Path

from modsync.steam import vdf
from modsync.steam.steamconfig import SteamConfig
from tests import fakesteam


class SteamConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = fakesteam.make_steam(
            Path(tmp.name) / "Steam",
            mapping={2074920: {"name": "GE-Proton10-34", "config": "", "priority": "250"}},
        )
        self.path = self.root / "config" / "config.vdf"

    def test_read_mapping(self):
        cfg = SteamConfig.load(self.path)
        self.assertEqual(cfg.compat_tool_name(2074920), "GE-Proton10-34")
        self.assertIsNone(cfg.compat_tool_name(489830))
        self.assertEqual(cfg.compat_tool(2074920)["priority"], "250")

    def test_set_and_remove_round_trip_untouched_content(self):
        before = self.path.read_text()
        cfg = SteamConfig.load(self.path)
        cfg.set_compat_tool(489830, "modsync_489830_hub")
        cfg.save()
        self.assertEqual(self.path.with_name("config.vdf.modsync-bak").read_text(), before)
        again = SteamConfig.load(self.path)
        self.assertEqual(again.compat_tool_name(489830), "modsync_489830_hub")
        self.assertEqual(again.compat_tool_name(2074920), "GE-Proton10-34")
        self.assertEqual(
            again.data["InstallConfigStore"]["Software"]["Valve"]["Steam"]["AutoUpdateWindowEnabled"], "0"
        )
        again.remove_compat_tool(489830)
        again.save()
        self.assertEqual(self.path.read_text(), before)  # exact restoration

    def test_restore_entry_verbatim_or_remove(self):
        cfg = SteamConfig.load(self.path)
        cfg.set_compat_entry(489830, {"name": "proton_experimental", "config": "x", "priority": "250"})
        self.assertEqual(cfg.compat_tool(489830), {"name": "proton_experimental", "config": "x", "priority": "250"})
        cfg.set_compat_entry(489830, None)
        self.assertIsNone(cfg.compat_tool(489830))

    def test_creates_missing_blocks(self):
        p = Path(self.root, "config", "empty.vdf")
        p.write_text(vdf.dumps({"InstallConfigStore": {"Software": {"Valve": {"Steam": {}}}}}) + "\n")
        cfg = SteamConfig.load(p)
        self.assertIsNone(cfg.mapping())
        cfg.set_compat_tool(1, "x")
        cfg.save(backup=False)
        self.assertEqual(SteamConfig.load(p).compat_tool_name(1), "x")
        self.assertFalse(p.with_name("empty.vdf.modsync-bak").exists())


if __name__ == "__main__":
    unittest.main()
