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

    arm64 = False

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.root = fakesteam.make_steam(
            self.tmp / "Steam", mapping=self.mapping, with_mo2lint=self.with_mo2lint, arm64=self.arm64
        )
        env = launchhook.SteamEnv(self.root, libs.all_libraries([self.root]))
        self.steam_running = False
        for p in (
            patch.object(launchhook, "steam_env", lambda: env),
            patch.object(shortcuts, "steam_is_running", lambda: self.steam_running),
            patch.object(launchhook, "modsync_command", lambda: ["/usr/bin/flatpak", "run", "io.github.skjiisa.ModSync"]),
            patch.object(launchhook.time, "sleep", lambda s: None),
            # The host's own architecture must not leak in (the suite also runs on a Steam Frame).
            patch("modsync.steam.compattools.platform.machine", return_value="aarch64" if self.arm64 else "x86_64"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.tool_dir = self.root / "compatibilitytools.d" / "modsync_489830_proton"

    def mapping_name(self):
        return SteamConfig.load(self.root / "config/config.vdf").compat_tool_name(489830)


class EnableOnArm64(LaunchHookBase):
    """On the Steam Frame, "Proton Experimental" in Steam runs the ARM64 build;
    the hook has to hand off to that one, not the x86_64 build."""

    arm64 = True
    mapping = {489830: {"name": "proton_experimental", "config": "", "priority": "250"}}

    def test_an_x86_64_selection_hands_off_to_steams_arm64_default(self):
        import shutil

        shutil.rmtree(self.root / "steamapps/common/Proton - Experimental (ARM64)")
        arm11 = fakesteam.install_tool(self.root, 4628740, "Proton 11.0 (ARM64)")
        launchhook.enable()
        self.assertIn(f'underlying="{arm11}"', (self.tool_dir / "proton").read_text())
        self.assertEqual(launchhook.game_proton().path, arm11)

    def test_hands_off_to_the_arm64_proton(self):
        launchhook.enable()
        arm_dir = self.root / "steamapps/common/Proton - Experimental (ARM64)"
        self.assertIn(f'underlying="{arm_dir}"', (self.tool_dir / "proton").read_text())
        self.assertEqual(launchhook.game_proton().path, arm_dir)


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
        self.assertIn('"modsync_489830_proton"', (self.tool_dir / "compatibilitytool.vdf").read_text())
        self.assertIn('"display_name" "ModSync (Skyrim Special Edition)"', (self.tool_dir / "compatibilitytool.vdf").read_text())
        self.assertNotIn("require_tool_appid", (self.tool_dir / "toolmanifest.vdf").read_text())
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
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
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        msg = launchhook.disable()
        self.assertIn("Steam's default", msg)
        self.assertIsNone(self.mapping_name())

    def test_global_default_wins_over_guess(self):
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(0, "GE-Proton10-34")
        cfg.save(backup=False)
        launchhook.enable()
        self.assertEqual(launchhook.Record.load().underlying_name, "GE-Proton10-34")


class GameProton(LaunchHookBase):
    mapping = {489830: GE}

    def test_resolves_steams_choice_through_the_hook(self):
        tool = launchhook.game_proton()
        self.assertEqual(tool.name, "GE-Proton10-34")
        launchhook.enable()
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        self.assertEqual(launchhook.game_proton().name, "GE-Proton10-34")
        launchhook.disable()
        self.assertEqual(launchhook.game_proton().name, "GE-Proton10-34")


class GameProtonWithMo2Lint(LaunchHookBase):
    with_mo2lint = True
    mapping = {489830: "mo2_489830_redirector"}

    def test_off_status_names_mo2lint(self):
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(489830, "mo2_489830_redirector")
        cfg.save(backup=False)
        self.assertEqual(
            launchhook.status().summary(), "Off. Steam's Play button opens Mod Organizer 2 (MO2-LINT) directly."
        )

    def test_redirector_is_not_a_proton(self):
        self.assertIsNone(launchhook.game_proton())
        launchhook.enable()
        self.assertIsNone(launchhook.game_proton())


class GameProtonWithCurrentMo2Lint(LaunchHookBase):
    """MO2-LINT 7.0.2 renamed its tool to mo2_<appid>_proton_redirector."""
    with_mo2lint = "mo2_489830_proton_redirector"
    mapping = {489830: {"name": "mo2_489830_proton_redirector", "config": "", "priority": "250"}}

    def test_off_status_names_mo2lint(self):
        self.assertEqual(
            launchhook.status().summary(), "Off. Steam's Play button opens Mod Organizer 2 (MO2-LINT) directly."
        )

    def test_hook_chains_to_it_and_disabling_restores_it(self):
        self.assertIsNone(launchhook.game_proton())
        launchhook.enable()
        self.assertEqual(launchhook.Record.load().underlying_name, "mo2_489830_proton_redirector")
        self.assertEqual(launchhook.status().continue_label, "Continue to Mod Organizer")
        self.assertIsNone(launchhook.game_proton())
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "mo2_489830_proton_redirector")


class GameProtonUnmapped(LaunchHookBase):
    def test_no_mapping_means_none(self):
        self.assertIsNone(launchhook.game_proton())


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
        self.assertIn("Waiting for Steam to close", st.summary())
        self.assertIsNone(launchhook.apply_pending())  # still running
        self.steam_running = False
        out = launchhook.apply_pending()
        self.assertIn("selected", out)
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        self.assertIsNone(launchhook.Pending.load())
        self.assertTrue(launchhook.status().enabled)

    def test_disable_queues_restore_and_removal(self):
        launchhook.enable()
        self.steam_running = True
        msg = launchhook.disable()
        self.assertIn("queued", msg)
        self.assertTrue(self.tool_dir.exists())  # Steam still maps to it: keep it launchable
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        self.steam_running = False
        out = launchhook.apply_pending()
        self.assertIn("GE-Proton10-34", out)
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.tool_dir.exists())
        self.assertIsNone(launchhook.Record.load())

    def test_queued_select_already_picked_by_hand_is_dropped(self):
        """Found on a Steam Frame, where Steam can't be closed: the user picks the
        hook under Properties, then Compatibility instead of waiting for the switch."""
        self.steam_running = True
        launchhook.enable()
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(489830, "modsync_489830_proton")
        cfg.save(backup=False)
        st = launchhook.status()
        self.assertTrue(st.enabled)
        self.assertIsNone(st.pending)
        self.assertTrue(st.summary().startswith("On."))
        self.assertIsNone(launchhook.Pending.load())
        # A later choice of their own must survive Steam closing.
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(489830, "GE-Proton10-34")
        cfg.save(backup=False)
        self.steam_running = False
        self.assertIsNone(launchhook.apply_pending())
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")

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
        self.assertIn("Properties, then Compatibility", st.summary())
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


class LegacyName(LaunchHookBase):
    """Hooks before the rename were called modsync_<appid>_hub. Steam Cloud only
    maps Windows save paths into the prefix for a tool whose internal name
    contains "proton", so under that name saves silently stopped syncing."""

    mapping = {489830: GE}

    def setUp(self):
        super().setUp()
        self.legacy_dir = self.root / "compatibilitytools.d" / "modsync_489830_hub"

    def install_legacy(self, *, selected=True):
        """What an older ModSync left behind: the hook under its old name."""
        launchhook.enable()
        self.tool_dir.rename(self.legacy_dir)
        vdf = self.legacy_dir / "compatibilitytool.vdf"
        vdf.write_text(vdf.read_text().replace("modsync_489830_proton", "modsync_489830_hub"))
        record = launchhook.Record.load()
        record.tool_id, record.tool_path = "modsync_489830_hub", str(self.legacy_dir)
        record.save()
        record.write_marker()
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(489830, "modsync_489830_hub" if selected else "GE-Proton10-34")
        cfg.save(backup=False)

    def test_the_tool_name_contains_proton(self):
        self.assertIn("proton", launchhook.tool_id(489830).lower())

    def test_status_flags_the_old_name(self):
        self.install_legacy()
        st = launchhook.status()
        self.assertTrue(st.enabled)
        self.assertTrue(st.legacy)
        self.assertIn("Steam Cloud", st.summary())
        self.assertEqual(launchhook.game_proton().name, "GE-Proton10-34")

    def test_upgrade_with_steam_closed_renames_and_switches(self):
        self.install_legacy()
        msg = launchhook.upgrade()
        self.assertIn("Steam Cloud", msg)
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        self.assertTrue((self.tool_dir / launchhook.MARKER).exists())
        self.assertIn('"modsync_489830_proton"', (self.tool_dir / "compatibilitytool.vdf").read_text())
        self.assertFalse(self.legacy_dir.exists())
        record = launchhook.Record.load()
        self.assertEqual(record.tool_id, "modsync_489830_proton")
        self.assertEqual(record.previous, GE)  # what the user had before ModSync, not the old hook
        self.assertFalse(launchhook.status().legacy)
        self.assertIsNone(launchhook.upgrade())  # nothing left to do
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")

    def test_upgrade_with_steam_running_keeps_the_old_hook_until_the_switch(self):
        self.install_legacy()
        self.steam_running = True
        msg = launchhook.upgrade()
        self.assertIn("queued", msg)
        self.assertEqual(self.mapping_name(), "modsync_489830_hub")
        self.assertTrue(self.legacy_dir.exists())  # Play still goes through it until Steam restarts
        self.assertTrue(self.tool_dir.exists())
        st = launchhook.status()
        self.assertEqual(st.pending.action, "select")
        self.assertIn("Steam Cloud", st.summary())
        self.steam_running = False
        out = launchhook.apply_pending()
        self.assertIn("renamed", out)
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        self.assertFalse(self.legacy_dir.exists())
        self.assertTrue(launchhook.status().enabled)

    def test_upgrade_keeps_the_modsync_the_hook_starts(self):
        """A development checkout must not repoint a Flatpak user's hook at itself."""
        import sys

        self.install_legacy()
        record = launchhook.Record.load()
        record.command = [sys.executable, "-m", "modsync"]
        record.save()
        record.write_marker()
        launchhook.upgrade()
        self.assertIn(f"modsync=({sys.executable} -m modsync)", (self.tool_dir / "proton").read_text())
        self.assertEqual(launchhook.Record.load().command, [sys.executable, "-m", "modsync"])

    def test_upgrade_when_not_selected_only_renames(self):
        self.install_legacy(selected=False)
        self.assertIsNone(launchhook.upgrade())
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.legacy_dir.exists())
        self.assertTrue((self.tool_dir / launchhook.MARKER).exists())
        self.assertEqual(launchhook.Record.load().tool_id, "modsync_489830_proton")

    def test_upgrade_retargets_a_select_queued_by_an_older_version(self):
        self.install_legacy(selected=False)
        launchhook.Pending("select", 489830, {"name": "modsync_489830_hub", "config": "", "priority": "250"}).save()
        launchhook.upgrade()
        self.assertEqual(launchhook.Pending.load().mapping["name"], "modsync_489830_proton")
        launchhook.apply_pending()
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")

    def test_disable_before_upgrade_restores_and_removes_the_old_hook(self):
        self.install_legacy()
        launchhook.disable()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.legacy_dir.exists())
        self.assertFalse(self.tool_dir.exists())

    def test_disable_queued_before_upgrade_removes_the_old_hook(self):
        self.install_legacy()
        self.steam_running = True
        launchhook.disable()
        self.steam_running = False
        launchhook.apply_pending()
        self.assertEqual(self.mapping_name(), "GE-Proton10-34")
        self.assertFalse(self.legacy_dir.exists())


class DaggerfallHook(LaunchHookBase):
    """For Daggerfall, Play runs Daggerfall Unity (a native program) and opens
    no window; the hook has its own record and queue next to Skyrim's."""

    DF = 1812390

    def setUp(self):
        super().setUp()
        self.dfu = self.tmp / "Daggerfall Unity" / "DaggerfallUnity.x86_64"
        fakesteam._executable(self.dfu, "#!/bin/sh\n")
        self.df_dir = self.root / "compatibilitytools.d" / "modsync_1812390_proton"

    def df_mapping(self):
        return SteamConfig.load(self.root / "config/config.vdf").compat_tool_name(self.DF)

    def test_enable_runs_daggerfall_unity_and_disable_restores_the_default(self):
        msg = launchhook.enable(self.DF, native=self.dfu)
        self.assertIn("starts Daggerfall Unity", msg)
        self.assertEqual(self.df_mapping(), "modsync_1812390_proton")
        script = (self.df_dir / "proton").read_text()
        self.assertIn(f"native='{self.dfu}'", script)
        self.assertIn('appid="1812390"', script)
        self.assertIn('"display_name" "ModSync (The Elder Scrolls II: Daggerfall)"',
                      (self.df_dir / "compatibilitytool.vdf").read_text())
        record = launchhook.Record.load(self.DF)
        self.assertEqual(record.native, str(self.dfu))
        self.assertTrue(launchhook.Record.path(self.DF).name.endswith("-1812390.json"))
        self.assertIsNone(launchhook.Record.load())  # Skyrim's record is a separate file
        st = launchhook.status(self.DF)
        self.assertTrue(st.enabled)
        self.assertEqual(st.summary(), "On. Steam's Play button on The Elder Scrolls II: Daggerfall starts Daggerfall Unity.")
        self.assertIn("Steam's default", launchhook.disable(self.DF))
        self.assertIsNone(self.df_mapping())
        self.assertFalse(self.df_dir.exists())
        self.assertIsNone(launchhook.Record.load(self.DF))

    def test_skyrims_script_runs_no_native_program(self):
        launchhook.enable()
        self.assertIn("native=''", (self.tool_dir / "proton").read_text())

    def test_both_hooks_live_side_by_side(self):
        launchhook.enable()
        launchhook.enable(self.DF, native=self.dfu)
        launchhook.disable(self.DF)
        self.assertTrue(launchhook.status().enabled)
        self.assertEqual(launchhook.Record.load().native, "")
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")

    def test_queued_switches_for_both_games_apply_together(self):
        self.steam_running = True
        self.assertIn("queued", launchhook.enable())
        self.assertIn("queued", launchhook.enable(self.DF, native=self.dfu))
        self.assertEqual({p.appid for p in launchhook.Pending.load_all()}, {489830, self.DF})
        self.assertIsNone(launchhook.apply_pending())
        self.steam_running = False
        out = launchhook.apply_pending()
        self.assertIn("Play now opens ModSync first", out)
        self.assertIn("Play on The Elder Scrolls II: Daggerfall now starts Daggerfall Unity", out)
        self.assertEqual(self.mapping_name(), "modsync_489830_proton")
        self.assertEqual(self.df_mapping(), "modsync_1812390_proton")
        self.assertEqual(launchhook.Pending.load_all(), [])

    def test_missing_program_is_reported(self):
        launchhook.enable(self.DF, native=self.dfu)
        self.dfu.unlink()
        self.assertIn("is missing, so Play starts the original", launchhook.status(self.DF).summary())

    def test_refresh_keeps_the_native_program(self):
        launchhook.enable(self.DF, native=self.dfu)
        record = launchhook.Record.load(self.DF)
        record.template_version = 1
        record.save()
        self.assertTrue(launchhook.refresh_if_outdated(self.DF))
        self.assertIn(f"native='{self.dfu}'", (self.df_dir / "proton").read_text())
        self.assertEqual(launchhook.Record.load(self.DF).native, str(self.dfu))


class DaggerfallOverLuxtorpeda(LaunchHookBase):
    """Steam already starts Daggerfall with Luxtorpeda, a launcher with no
    Proton in it (it runs its own Daggerfall Unity), as many Decks have it."""

    DF = DaggerfallHook.DF
    LUX = {"name": "luxtorpeda", "config": "", "priority": "250"}
    mapping = {1812390: LUX}
    df_mapping = DaggerfallHook.df_mapping

    def setUp(self):
        super().setUp()
        self.dfu = self.tmp / "Daggerfall Unity" / "DaggerfallUnity.x86_64"
        fakesteam._executable(self.dfu, "#!/bin/sh\n")
        lux = self.root / "compatibilitytools.d" / "luxtorpeda"
        fakesteam._executable(lux / "luxtorpeda.sh", "#!/bin/sh\n")
        (lux / "compatibilitytool.vdf").write_text(
            '"compatibilitytools"\n{\n  "compat_tools"\n  {\n    "luxtorpeda"\n    {\n'
            '      "install_path" "."\n      "display_name" "Luxtorpeda"\n'
            '      "from_oslist"  "windows"\n      "to_oslist"    "linux"\n    }\n  }\n}\n'
        )

    def test_status_names_it(self):
        self.assertEqual(launchhook.status(self.DF).summary(),
                         "Off. Steam's Play button starts The Elder Scrolls II: Daggerfall with Luxtorpeda.")

    def test_enable_hands_the_rest_to_steams_default_and_disable_restores_it(self):
        launchhook.enable(self.DF, native=self.dfu)
        record = launchhook.Record.load(self.DF)
        self.assertEqual(record.underlying_name, "proton_experimental")
        self.assertEqual(record.previous, self.LUX)
        self.assertEqual(self.df_mapping(), "modsync_1812390_proton")
        self.assertIn("back to luxtorpeda", launchhook.disable(self.DF))
        self.assertEqual(self.df_mapping(), "luxtorpeda")

    def test_luxtorpeda_as_steams_default_too_falls_back_to_valves_proton(self):
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(0, "luxtorpeda")
        cfg.save(backup=False)
        launchhook.enable(self.DF, native=self.dfu)
        self.assertEqual(launchhook.Record.load(self.DF).underlying_name, "proton_experimental")
        self.assertIn("back to luxtorpeda", launchhook.disable(self.DF))

    def test_without_a_native_program_it_still_wants_a_proton(self):
        with self.assertRaisesRegex(RuntimeError, "has no 'proton' to run"):
            launchhook.enable(self.DF)


class DaggerfallHookFollowsInstall(DaggerfallHook):
    """Installing Daggerfall Unity elsewhere moves the hook only while Steam
    uses it, and never undoes a choice made since (review of #112)."""

    def setUp(self):
        super().setUp()
        from modsync import dfu

        self.dfu_mod = dfu
        self.moved = dfu.Install(self.tmp / "Moved")
        fakesteam._executable(self.moved.executable, "#!/bin/sh\n")

    def script_native(self):
        import shlex

        line = next(l for l in (self.df_dir / "proton").read_text().splitlines() if l.startswith("native="))
        return shlex.split(line.removeprefix("native="))[0]

    def test_follows_while_on(self):
        launchhook.enable(self.DF, native=self.dfu)
        self.assertIn("refreshed", self.dfu_mod.follow_install(self.moved))
        self.assertEqual(self.script_native(), str(self.moved.executable))
        self.assertEqual(launchhook.Record.load(self.DF).native, str(self.moved.executable))

    def test_follows_a_switch_queued_while_steam_runs(self):
        self.steam_running = True
        launchhook.enable(self.DF, native=self.dfu)
        self.assertIn("queued", self.dfu_mod.follow_install(self.moved))
        self.assertEqual(self.script_native(), str(self.moved.executable))
        self.assertEqual(launchhook.Pending.load(self.DF).action, "select")
        self.assertIsNone(self.df_mapping())  # still Steam's default until it closes

    def test_a_queued_disable_survives(self):
        launchhook.enable(self.DF, native=self.dfu)
        self.steam_running = True
        launchhook.disable(self.DF)
        self.assertIsNone(self.dfu_mod.follow_install(self.moved))
        self.assertEqual(launchhook.Pending.load(self.DF).action, "restore")
        self.assertIn("restore the previous launcher", launchhook.status(self.DF).summary())

    def test_another_launcher_picked_in_steam_stays(self):
        launchhook.enable(self.DF, native=self.dfu)
        cfg = SteamConfig.load(self.root / "config/config.vdf")
        cfg.set_compat_tool(self.DF, "GE-Proton10-34")
        cfg.save()
        self.assertIsNone(self.dfu_mod.follow_install(self.moved))
        self.assertEqual(self.df_mapping(), "GE-Proton10-34")

    def test_the_installed_script_wins_over_another_modsyncs_record(self):
        # The native ModSync enables copy A; the Flatpak, with its own config, copy B.
        launchhook.enable(self.DF, native=self.dfu)
        own_config = os.environ["XDG_CONFIG_HOME"]
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "flatpak-config")
        launchhook.enable(self.DF, native=self.moved.executable)
        os.environ["XDG_CONFIG_HOME"] = own_config
        self.assertEqual(launchhook.Record.load(self.DF).native, str(self.dfu))  # stale, local
        self.assertEqual(launchhook.status(self.DF).native, str(self.moved.executable))
        a = self.dfu_mod.Install(self.dfu.parent)
        self.assertIn("refreshed", self.dfu_mod.follow_install(a))
        self.assertEqual(self.script_native(), str(self.dfu))


class HandoffTests(unittest.TestCase):
    def test_fields_end_in_nul_and_keep_spaces_and_unicode(self):
        from modsync.mo2.launch import LaunchPlan

        command = ["/home/me/Mod Organizer/ModOrganizer.exe", "-p", "Légendaire", "run", "-e", "SKSE"]
        plan = LaunchPlan([], {}, Path("/home/me/Mod Organizer"), "SKSE", True, Path("/p/Proton 9.0"), command)
        fields = launchhook.encode_handoff(plan).split(b"\0")
        self.assertEqual(fields[-1], b"")  # each field ends in a NUL, as `mapfile -d ''` expects
        self.assertEqual([f.decode() for f in fields[:-1]],
                         [launchhook.HANDOFF_TAG, "/p/Proton 9.0", "/home/me/Mod Organizer", *command])

    def test_a_plan_without_proton_or_command_is_refused(self):
        from modsync.mo2.launch import LaunchPlan

        for plan in (LaunchPlan([], {}, Path("/i"), "MO2", False, None, ["/i/ModOrganizer.exe"]),
                     LaunchPlan([], {}, Path("/i"), "MO2", False, Path("/p"), [])):
            with self.assertRaises(ValueError):
                launchhook.encode_handoff(plan)

    def test_the_template_tag_matches(self):
        text = (launchhook.TEMPLATE_DIR / "proton").read_text()
        self.assertIn("@@HANDOFF_TAG@@", text)


if __name__ == "__main__":
    unittest.main()
