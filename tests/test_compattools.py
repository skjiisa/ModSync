import tempfile
import unittest
from pathlib import Path

from modsync.steam import compattools
from modsync.steam import libraries as libs
from tests import fakesteam


class CompatToolDiscovery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = fakesteam.make_steam(Path(tmp.name) / "Steam", with_mo2lint=True)
        self.libraries = libs.all_libraries([self.root])

    def test_custom_tools_read_compatibilitytool_vdf_with_comments(self):
        names = {t.name: t for t in compattools.custom_tools(self.root)}
        self.assertIn("GE-Proton10-34", names)
        self.assertEqual(names["GE-Proton10-34"].path, self.root / "compatibilitytools.d" / "GE-Proton10-34")
        self.assertEqual(names["GE-Proton10-34"].kind, "custom")
        self.assertTrue(names["mo2_489830_redirector"].is_mo2lint)
        self.assertFalse(names["GE-Proton10-34"].is_mo2lint)

    def test_loose_manifest_with_relative_install_path(self):
        tools = self.root / "compatibilitytools.d"
        (tools / "stl-dist").mkdir()
        (tools / "stl.vdf").write_text(
            '"compatibilitytools" { "compat_tools" { "Proton-stl" { "install_path" "stl-dist" '
            '"display_name" "Steam Tinker Launch" "from_oslist" "windows" "to_oslist" "linux" } } }'
        )
        found = {t.name: t for t in compattools.custom_tools(self.root)}
        self.assertEqual(found["Proton-stl"].path, (tools / "stl-dist").resolve())
        self.assertEqual(found["Proton-stl"].display_name, "Steam Tinker Launch")

    def test_valve_tools_come_from_steam_play_manifests_and_must_be_installed(self):
        valve = {t.name: t for t in compattools.valve_tools(self.root, self.libraries)}
        self.assertEqual(list(valve), ["proton_experimental"])  # proton_9 is not installed
        self.assertEqual(valve["proton_experimental"].path, self.root / "steamapps/common/Proton - Experimental")
        self.assertEqual(valve["proton_experimental"].display_name, "Proton Experimental")

    def test_find_and_default(self):
        self.assertEqual(compattools.find_tool("GE-Proton10-34", self.root, self.libraries).kind, "custom")
        self.assertIsNone(compattools.find_tool("nope", self.root, self.libraries))
        self.assertEqual(compattools.default_valve_tool(self.root, self.libraries).name, "proton_experimental")
        self.assertEqual(compattools.mo2lint_tool(489830, self.root).display_name, "MO2 Skyrim Special Edition")
        self.assertIsNone(compattools.mo2lint_tool(377160, self.root))

    def test_require_tool_appid_and_runtime_path(self):
        proton = self.root / "steamapps/common/Proton - Experimental"
        self.assertEqual(compattools.require_tool_appid(proton), fakesteam.SLR4_APPID)
        self.assertEqual(
            compattools.runtime_path(fakesteam.SLR4_APPID, self.libraries),
            self.root / "steamapps/common/SteamLinuxRuntime_4",
        )
        self.assertIsNone(compattools.runtime_path(fakesteam.SNIPER_APPID, self.libraries))
        self.assertIsNone(compattools.require_tool_appid(self.root / "nowhere"))


if __name__ == "__main__":
    unittest.main()
