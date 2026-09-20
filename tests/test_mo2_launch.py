"""MO2 launch plans use the right instance, profile, Proton and virtual filesystem."""

from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from modsync.mo2 import launch
from modsync.steam import vdf
from tests import fakesteam


class LaunchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = fakesteam.make_steam(self.tmp / "Steam")
        self.common = self.root / "steamapps/common"
        self.game = self.common / "Skyrim Special Edition"
        (self.game / "SkyrimSE.exe").touch()
        self.proton = self.common / "Proton - Experimental"
        self.compat = self.root / "steamapps/compatdata/489830"
        (self.compat / "pfx").mkdir(parents=True)
        (self.compat / "config_info").write_text(f"version\n{self.proton}/files/lib/\n")
        self.instance = self.tmp / "MO2 with spaces"
        self.instance.mkdir()
        (self.instance / "ModOrganizer.exe").touch()
        self.ini = self.instance / "ModOrganizer.ini"
        self.ini.write_text("[General]\ngameName=Skyrim Special Edition\nselected_profile=My Profile\n")
        for item in (
            patch("modsync.mo2.launch.platforms.current", return_value=Mock(steam_roots=lambda: [self.root])),
            patch("modsync.mo2.launch.shortcuts.steam_is_running", return_value=True),
            patch("modsync.mo2.launch.background.in_flatpak", return_value=False),
            patch("modsync.mo2.launch.state_dir", return_value=self.tmp / "logs"),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_open_uses_existing_prefix_runtime_and_selected_profile(self):
        plan = launch.build_plan(self.instance)
        self.assertEqual(plan.argv[:3], [str(self.common / "SteamLinuxRuntime_4/_v2-entry-point"), "--verb=run", "--"])
        self.assertEqual(plan.argv[3:], [str(self.proton / "proton"), "run", str(self.instance / "ModOrganizer.exe"), "-i", "", "-p", "My Profile"])
        self.assertEqual(plan.env["STEAM_COMPAT_DATA_PATH"], str(self.compat))
        self.assertEqual(plan.env["SteamAppId"], "489830")
        self.assertEqual(plan.cwd, self.instance)

    def test_play_prefers_renamed_configured_skse_and_preserves_arguments(self):
        with self.ini.open("a") as file:
            file.write('[customExecutables]\n1\\title=My SKSE\n1\\binary=Z:/game/skse64_loader.exe\n1\\arguments=--keep-this\n2\\title=Skyrim\n2\\binary=Z:/game/SkyrimSE.exe\n')
        before = self.ini.read_bytes()
        plan = launch.build_plan(self.instance, play=True)
        self.assertEqual(plan.argv[-3:], ["run", "-e", "My SKSE"])
        self.assertEqual(plan.target, "My SKSE")
        self.assertEqual(self.ini.read_bytes(), before)

    def test_play_without_saved_executables_still_goes_through_mo2(self):
        for filename in ("SkyrimSE.exe", "skse64_loader.exe"):
            (self.game / filename).touch()
            plan = launch.build_plan(self.instance, play=True)
            self.assertIn(str(self.instance / "ModOrganizer.exe"), plan.argv)
            self.assertEqual(plan.argv[-4:], ["run", "-c", "Z:" + str(self.game), "Z:" + str(self.game / filename)])

    def test_external_library_prefix_is_not_confused_with_steam_root(self):
        external = self.tmp / "Other Library"
        (external / "steamapps").mkdir(parents=True)
        (self.root / "steamapps/appmanifest_489830.acf").replace(external / "steamapps/appmanifest_489830.acf")
        self.compat.rename(external / "steamapps/compatdata-game")
        (external / "steamapps/compatdata").mkdir()
        (external / "steamapps/compatdata-game").rename(external / "steamapps/compatdata/489830")
        (self.root / "steamapps/libraryfolders.vdf").write_text(vdf.dumps({"libraryfolders": {
            "0": {"path": str(self.root)}, "1": {"path": str(external)}}}))
        plan = launch.build_plan(self.instance)
        self.assertEqual(plan.env["STEAM_COMPAT_DATA_PATH"], str(external / "steamapps/compatdata/489830"))
        self.assertEqual(plan.env["STEAM_COMPAT_CLIENT_INSTALL_PATH"], str(self.root))

    def test_missing_runtime_or_proton_explains_how_to_fix_it(self):
        runtime = self.common / "SteamLinuxRuntime_4/_v2-entry-point"
        runtime.unlink()
        with self.assertRaisesRegex(RuntimeError, "Runtime.*missing"):
            launch.build_plan(self.instance)
        (self.compat / "config_info").write_text("/gone/Proton/files/lib/\n")
        with self.assertRaisesRegex(RuntimeError, "last-used Proton"):
            launch.build_plan(self.instance)

    def test_missing_instance_and_steam_update_are_blocked(self):
        with self.assertRaisesRegex(RuntimeError, "Choose an MO2"):
            launch.build_plan(None)
        manifest = self.root / "steamapps/appmanifest_489830.acf"
        data = vdf.load(manifest)
        data["AppState"]["StateFlags"] = "1024"
        manifest.write_text(vdf.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "Steam is updating"):
            launch.build_plan(self.instance, play=True)

    def test_steam_must_be_running_to_play_but_not_to_open_mo2(self):
        with patch("modsync.mo2.launch.shortcuts.steam_is_running", return_value=False):
            launch.build_plan(self.instance)
            with self.assertRaisesRegex(RuntimeError, "Start Steam"):
                launch.build_plan(self.instance, play=True)

    def test_flatpak_launch_escapes_to_host_without_shell_interpolation(self):
        plan = launch.build_plan(self.instance, play=True)
        with patch("modsync.mo2.launch.background.in_flatpak", return_value=True), patch.object(launch.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            launcher = launch.Launcher()
            launcher.start(plan)
            argv = popen.call_args.args[0]
            self.assertEqual(argv[:4], ["flatpak-spawn", "--host", f"--directory={self.instance}", "env"])
            self.assertEqual(argv[-len(plan.argv):], plan.argv)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            with self.assertRaisesRegex(RuntimeError, "already running"):
                launcher.start(plan)

    def test_failed_child_is_reported_and_can_be_retried(self):
        plan = launch.LaunchPlan([sys.executable, "-c", "raise SystemExit(7)"], {}, self.tmp, "SKSE", True)
        launcher = launch.Launcher()
        launcher.start(plan)
        deadline = time.monotonic() + 5
        while launcher.running() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(launcher.running())
        errors = launcher.poll()
        self.assertEqual(len(errors), 1)
        self.assertIn("SKSE exited with code 7", errors[0])
        self.assertEqual(launcher.poll(), [])
        self.assertTrue((self.tmp / "logs/mo2-launch.log").exists())
