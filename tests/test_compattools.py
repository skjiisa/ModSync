import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class SteamDefaultTool(unittest.TestCase):
    """What Steam runs for a game left on "Default" in its Compatibility menu."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = fakesteam.make_steam(Path(tmp.name) / "Steam")
        self.libraries = libs.all_libraries([self.root])
        variant = patch.object(compattools.steamos, "variant", return_value=None)
        variant.start()
        self.addCleanup(variant.stop)

    def default(self, global_choice=None):
        return compattools.steam_default_tool(489830, self.root, self.libraries, global_choice=global_choice)

    def test_proton_stable_beats_experimental(self):
        self.assertEqual(self.default().name, "proton_experimental")  # stable not installed: last resort
        fakesteam.install_tool(self.root, 4628710, "Proton 11.0")
        self.assertEqual(self.default().name, "proton_11")

    def test_global_choice_beats_proton_stable(self):
        fakesteam.install_tool(self.root, 4628710, "Proton 11.0")
        self.assertEqual(self.default("GE-Proton10-34").name, "GE-Proton10-34")
        self.assertEqual(self.default("not-installed").name, "proton_11")

    def test_device_profile_and_manifest_mapping_beat_the_global_choice(self):
        fakesteam.install_tool(self.root, 4628710, "Proton 11.0")
        game = {"appid": 489830, "common": {"steam_deck_compatibility": {
            "configuration": {"recommended_runtime": "proton-11.0-2RC"}}}}
        fakesteam.write_appinfo(self.root, apps={489830: game})
        self.assertEqual(self.default("GE-Proton10-34").name, "GE-Proton10-34")  # not on a Deck
        with patch.object(compattools.steamos, "variant", return_value="steamdeck"):
            self.assertEqual(self.default("GE-Proton10-34").name, "proton_11")

        manifests = {**fakesteam.STEAM_PLAY_MANIFESTS}
        manifests["extended"] = {**manifests["extended"], "app_mappings": {"489830": {"tool": "proton_experimental"}}}
        with patch.object(fakesteam, "STEAM_PLAY_MANIFESTS", manifests):
            fakesteam.write_appinfo(self.root)
        self.assertEqual(self.default("GE-Proton10-34").name, "proton_experimental")

    def test_find_tool_matches_aliases(self):
        self.assertEqual(compattools.find_tool("proton-experimental", self.root, self.libraries).name, "proton_experimental")


class Arm64CompatTools(unittest.TestCase):
    """Steam on ARM64 (the Steam Frame) lists Valve's ARM64 Protons in a second
    manifests app and runs the ``-arm64`` counterpart of whatever is selected."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = fakesteam.make_steam(Path(tmp.name) / "Steam", arm64=True)
        self.libraries = libs.all_libraries([self.root])
        self.arm_dir = self.root / "steamapps/common/Proton - Experimental (ARM64)"

    def arm64(self, on: bool = True):
        return patch.object(compattools.platform, "machine", return_value="aarch64" if on else "x86_64")

    def test_valve_tools_include_the_arm64_manifest(self):
        valve = {t.name: t for t in compattools.valve_tools(self.root, self.libraries)}
        self.assertEqual(list(valve), ["proton_experimental", "proton-experimental-arm64"])
        self.assertEqual(valve["proton-experimental-arm64"].path, self.arm_dir)
        self.assertEqual(valve["proton-experimental-arm64"].aliases, ("proton-experimental",))

    def test_candidates_follow_steam(self):
        self.assertEqual(
            compattools.arm64_candidates("proton_experimental", self.root),
            ["proton_experimental-arm64", "proton-experimental-arm64", "proton_experimental"],
        )
        self.assertEqual(compattools.arm64_candidates("proton_11", self.root), ["proton_11-arm64", "proton_11"])
        self.assertEqual(compattools.arm64_candidates("proton-experimental-arm64", self.root), ["proton-experimental-arm64"])
        self.assertEqual(
            compattools.arm64_candidates("GE-Proton11-7-aarch64", self.root),
            ["GE-Proton11-7-aarch64-arm64", "GE-Proton11-7-aarch64"],
        )

    def test_selected_x86_name_resolves_to_the_arm64_build_on_arm64(self):
        with self.arm64():
            self.assertEqual(compattools.find_tool("proton_experimental", self.root, self.libraries).path, self.arm_dir)
            self.assertEqual(compattools.find_tool("GE-Proton10-34", self.root, self.libraries).kind, "custom")
            self.assertEqual(compattools.default_valve_tool(self.root, self.libraries).name, "proton-experimental-arm64")
        with self.arm64(False):
            self.assertEqual(
                compattools.find_tool("proton_experimental", self.root, self.libraries).name, "proton_experimental"
            )
            self.assertEqual(compattools.default_valve_tool(self.root, self.libraries).name, "proton_experimental")

    def test_default_is_proton_stable_arm64_like_on_the_frame(self):
        """Seen on a Steam Frame: Default with Proton 11.0 (ARM64) installed maps
        the game to proton-stable, which is proton_11-arm64."""
        fakesteam.install_tool(self.root, 4628740, "Proton 11.0 (ARM64)")
        with self.arm64(), patch.object(compattools.steamos, "variant", return_value="vr"):
            tool = compattools.steam_default_tool(489830, self.root, self.libraries)
        self.assertEqual(tool.name, "proton_11-arm64")
        self.assertEqual(tool.path, self.root / "steamapps/common/Proton 11.0 (ARM64)")

    def test_arm64_falls_back_to_the_name_as_given(self):
        import shutil

        shutil.rmtree(self.arm_dir)
        with self.arm64():
            self.assertEqual(
                compattools.find_tool("proton_experimental", self.root, self.libraries).name, "proton_experimental"
            )
            self.assertEqual(compattools.default_valve_tool(self.root, self.libraries).name, "proton_experimental")


if __name__ == "__main__":
    unittest.main()
