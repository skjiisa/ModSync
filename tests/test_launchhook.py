"""Enable/disable the Steam launch hook against a fake Steam install."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import launchhook
from modsync.steam import libraries as libs
from modsync.steam import shortcuts
from modsync.steam.steamconfig import SteamConfig
from tests import fakesteam

GE = {"name": "GE-Proton10-34", "config": "", "priority": "250"}


class LaunchHookBase(unittest.TestCase):
    with_mo2lint = False
    mapping: dict = {}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.root = fakesteam.make_steam(self.tmp / "Steam", mapping=self.mapping, with_mo2lint=self.with_mo2lint)
        env = launchhook.SteamEnv(self.root, libs.all_libraries([self.root]))
        self.steam_running = False
        for p in (
            patch.object(launchhook, "steam_env", lambda: env),
            patch.object(shortcuts, "steam_is_running", lambda: self.steam_running),
            patch.object(launchhook, "modsync_command", lambda: ["/usr/bin/flatpak", "run", "io.github.skjiisa.ModSync"]),
            patch.object(launchhook.time, "sleep", lambda s: None),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.tool_dir = self.root / "compatibilitytools.d" / "modsync_489830_hub"

    def mapping_name(self):
        return SteamConfig.load(self.root / "config/config.vdf").compat_tool_name(489830)


class EnableWithSteamClosed(LaunchHookBase):
    mapping = {489830: GE}

    def test_renders_tool_and_selects_it(self):
        msg = launchhook.enable()
        self.assertIn("enabled", msg)
        self.assertTrue((self.tool_dir / launchhook.MARKER).exists())
        script = (self.tool_dir / "proton").read_text()
        self.assertNotIn("@@", script)
        self.assertIn(f'underlying="{self.root}/compatibilitytools.d/GE-Proton10-34"', script)
        self.assertIn("modsync=(/usr/bin/flatpak run io.github.skjiisa.ModSync)", script)
        self.assertIn(f"libraries=({self.root})", script)
        self.assertTrue(os.access(self.tool_dir / "proton", os.X_OK))
        self.assertIn('"modsync_489830_hub"', (self.tool_dir / "compatibilitytool.vdf").read_text())
        self.assertIn('"display_name" "ModSync (Skyrim Special Edition)"', (self.tool_dir / "compatibilitytool.vdf").read_text())
        self.assertNotIn("require_tool_appid", (self.tool_dir / "toolmanifest.vdf").read_text())
        self.assertEqual(self.mapping_name(), "modsync_489830_hub")
        record = launchhook.Record.load()
        self.assertEqual(record.previous, GE)
        self.assertEqual(record.underlying_name, "GE-Proton10-34")
        st = launchhook.status()
        self.assertTrue(st.enabled)
        self.assertIn("continues to the game (GE-Proton10-34)", st.summary())
        self.assertEqual(st.continue_label, "Continue to Skyrim Special Edition")

    def test_disable_restores_previous_and_removes_dir(self):
        launchhook.enable()
        msg = launchhook.disable()
        self.assertIn("back to GE-Proton10-34", msg)
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.tool_dir.exists())
        self.assertIsNone(launchhook.Record.load())
        self.assertFalse(launchhook.status().installed)

    def test_reenable_keeps_the_original_previous(self):
        launchhook.enable()
        msg = launchhook.enable()
        self.assertIn("refreshed", msg)
        self.assertEqual(launchhook.Record.load().previous, GE)
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")

    def test_through_overrides(self):
        launchhook.enable(through="proton_experimental")
        record = launchhook.Record.load()
        self.assertEqual(record.underlying_name, "proton_experimental")
        self.assertIn("Proton - Experimental", (self.tool_dir / "proton").read_text())
        with self.assertRaisesRegex(RuntimeError, "no compatibility tool named"):
            launchhook.enable(through="nope")

    def test_never_overwrites_a_foreign_directory(self):
        self.tool_dir.mkdir(parents=True)
        (self.tool_dir / "proton").write_text("someone else's")
        with self.assertRaisesRegex(RuntimeError, "not a ModSync launch hook"):
            launchhook.enable()
        with self.assertRaisesRegex(RuntimeError, "no ModSync marker"):
            launchhook.remove_tool_dir(self.tool_dir)
        self.assertEqual((self.tool_dir / "proton").read_text(), "someone else's")


class EnableWithNoExplicitChoice(LaunchHookBase):
    def test_falls_back_to_valve_default_and_restores_by_removing(self):
        launchhook.enable()
        record = launchhook.Record.load()
        self.assertEqual(record.underlying_name, "proton_experimental")
        self.assertIsNone(record.previous)
        self.assertEqual(self.mapping_name(), "modsync_489830_hub")
        msg = launchhook.disable()
        self.assertIn("Steam's default", msg)
        self.assertIsNone(self.mapping_name())

    def test_global_default_wins_over_guess(self):
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(0, "GE-Proton10-34")
        cfg.save(backup=False)
        launchhook.enable()
        self.assertEqual(launchhook.Record.load().underlying_name, "GE-Proton10-34")


class PrefersMo2Lint(LaunchHookBase):
    with_mo2lint = True
    mapping = {489830: GE}

    def test_chains_to_mo2lint_redirector_but_restores_ge(self):
        msg = launchhook.enable()
        self.assertIn("Mod Organizer 2 (MO2-LINT)", msg)
        record = launchhook.Record.load()
        self.assertEqual(record.underlying_name, "mo2_489830_redirector")
        self.assertEqual(record.previous, GE)
        st = launchhook.status()
        self.assertEqual(st.continue_label, "Continue to Mod Organizer")
        self.assertIn("continues to Mod Organizer 2 (MO2-LINT)", st.summary())
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")


class WithSteamRunning(LaunchHookBase):
    mapping = {489830: GE}

    def test_enable_queues_and_apply_pending_waits_for_steam(self):
        self.steam_running = True
        msg = launchhook.enable()
        self.assertIn("queued", msg)
        self.assertTrue(self.tool_dir.exists())
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")  # untouched while Steam runs
        st = launchhook.status()
        self.assertFalse(st.enabled)
        self.assertEqual(st.pending.action, "select")
        self.assertIn("Waiting for Steam to be closed", st.summary())
        self.assertIsNone(launchhook.apply_pending())  # still running
        self.steam_running = False
        out = launchhook.apply_pending()
        self.assertIn("selected", out)
        self.assertEqual(self.mapping_name(), "modsync_489830_hub")
        self.assertIsNone(launchhook.Pending.load())
        self.assertTrue(launchhook.status().enabled)

    def test_disable_queues_restore_and_removal(self):
        launchhook.enable()
        self.steam_running = True
        msg = launchhook.disable()
        self.assertIn("queued", msg)
        self.assertTrue(self.tool_dir.exists())  # Steam still maps to it: keep it launchable
        self.assertEqual(self.mapping_name(), "modsync_489830_hub")
        self.steam_running = False
        out = launchhook.apply_pending()
        self.assertIn("GE-Proton10-34", out)
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.tool_dir.exists())
        self.assertIsNone(launchhook.Record.load())

    def test_queued_select_is_dropped_if_files_vanished(self):
        self.steam_running = True
        launchhook.enable()
        launchhook.remove_tool_dir(self.tool_dir)
        self.steam_running = False
        self.assertIn("dropped", launchhook.apply_pending())
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")


class StatusEdgeCases(LaunchHookBase):
    mapping = {489830: GE}

    def test_installed_but_user_switched_steam_elsewhere(self):
        launchhook.enable()
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(489830, "GE-Proton10-34")
        cfg.save(backup=False)
        st = launchhook.status()
        self.assertTrue(st.installed)
        self.assertFalse(st.selected)
        self.assertIn("Properties → Compatibility", st.summary())
        # Turning it off from here just cleans up; Steam keeps its own choice.
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.tool_dir.exists())

    def test_refresh_if_outdated_rerenders_only_when_template_changed(self):
        launchhook.enable()
        self.assertFalse(launchhook.refresh_if_outdated())
        record = launchhook.Record.load()
        record.template_version = 0
        record.save()
        (self.tool_dir / "proton").write_text("stale")
        self.assertTrue(launchhook.refresh_if_outdated())
        self.assertIn("GE-Proton10-34", (self.tool_dir / "proton").read_text())
        self.assertEqual(launchhook.Record.load().template_version, launchhook.TEMPLATE_VERSION)

    def test_marker_carries_the_record_for_other_modsync_installs(self):
        launchhook.enable()
        marker = json.loads((self.tool_dir / launchhook.MARKER).read_text())
        self.assertEqual(marker["template_version"], launchhook.TEMPLATE_VERSION)
        self.assertEqual(marker["record"]["previous"], GE)
        # A Flatpak ModSync has its own config dir: no launch-hook.json there, but
        # the tool directory still tells it what the hook hands off to and how to undo.
        launchhook.Record.remove()
        st = launchhook.status()
        self.assertTrue(st.enabled)
        self.assertEqual(st.underlying_name, "GE-Proton10-34")
        self.assertTrue(st.underlying_exists)
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.tool_dir.exists())


if __name__ == "__main__":
    unittest.main()
