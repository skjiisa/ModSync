"""Build the window offscreen in each setup state and drive it. Catches wiring
mistakes (missing attributes, signals to dead slots) that only surface when
Qt runs, and pins down what each section offers in each state."""

import os
import time
from pathlib import Path
from threading import Event
from unittest.mock import patch

from modsync import gameversion, launchhook
from modsync.games import SKYRIM_SE
from modsync.service import ModSyncService, SyncStatus
from modsync.state import State
from tests.ui_support import UiTestCase, fake_game_status, handoff_plan


class SectionTests(UiTestCase):
    def test_nothing_set_up(self):
        window = self.window()
        home = window.pages["home"]
        self.assertIsNotNone(home.setup)  # the main action is the setup wizard
        self.assertFalse(home.play.isEnabled())
        self.assertIs(window.focusWidget(), home.setup)
        self.assertEqual(window.pages["game"].panel.version.text(), "1.7.104")
        self.assertIsNotNone(window.pages["mods"].chooser)
        self.assertTrue(hasattr(window.pages["sync"], "choose_first"))
        self.assertEqual(home.hook_tile.badge.text(), "Off")
        self.assertIn("Choose to turn it on", home.hook_tile.description)
        self.assertIsNone(window.pages["system"].reset_tile)

    def test_instance_only(self):
        State(instance_path=str(self.tmp), instance_label="MO2").save()
        window = self.window()
        home = window.pages["home"]
        self.assertTrue(home.play.isEnabled())
        self.assertEqual(home.play.text(), "Play Skyrim")
        sync = window.pages["sync"]
        self.assertFalse(sync.live)
        self.assertIsNotNone(sync.join)
        self.assertIsNotNone(window.pages["system"].reset_tile)

    def test_syncing(self):
        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        window = self.window()
        sync = window.pages["sync"]
        self.assertTrue(sync.live)
        self.assertIn("100% in sync", sync.folder_detail.text())
        self.assertEqual(sync.ring.value, 100)
        self.assertEqual(window.pages["home"].sync_row.badge.text(), "Up to date")

    def test_sections_switch_with_the_bumpers_and_wrap(self):
        from modsync.ui.input import Action, InputRouter

        window = self.window()
        router = InputRouter.instance()
        seen = []
        for _ in range(len(window.pages)):
            router.dispatch(Action.NEXT_TAB)
            seen.append(window._current)
        self.assertEqual(seen, ["game", "mods", "sync", "system", "home"])
        router.dispatch(Action.PREV_TAB)
        self.assertEqual(window._current, "system")
        self.assertTrue(window.tabs["system"].isChecked())
        self.assertTrue(window.pages["system"].isAncestorOf(window.focusWidget()))
        router.dispatch(Action.BACK)
        self.assertEqual(window._current, "home")

    def test_readiness_rows_open_the_section_that_fixes_them(self):
        window = self.window()
        home = window.pages["home"]
        self.assertEqual(home.game_row.badge.text(), "1.7.104")
        self.assertEqual(home.mo2_row.badge.text(), "Not chosen")
        home.mo2_row.click()
        self.assertEqual(window._current, "mods")
        window.go("home")
        home.game_row.click()
        self.assertEqual(window._current, "game")

    def test_opening_a_sheet_never_creates_a_second_window(self):
        window = self.window()
        window.confirm("Title", "Text", [("a", "A", "", "primary", None)], lambda key: None)
        self.settle()
        self.assertIs(self.app.activeWindow(), window)
        self.assertEqual([w for w in self.app.topLevelWidgets() if w.isVisible()], [window])


class GameCopyTests(UiTestCase):
    def _panel(self):
        window = self.window()
        return window.service, window.pages["game"].panel

    def test_first_run_warnings_are_readable_without_an_instance(self):
        service, panel = self._panel()
        installed = gameversion.GameVersion.parse("1.7.104")
        skse = gameversion.GameVersion.parse("1.5.97")
        check = gameversion.VersionCheck(installed, None, skse=gameversion.SkseCheck([
            gameversion.SkseFile(Path("skse64_1_5_97.dll"), skse, "game folder")]))
        with patch.object(service, "game_version_check", return_value=check):
            panel._on_game_status(fake_game_status())
        self.assertIn("1.5.97", panel.message.text())
        self.assertEqual(panel.message.property("role"), "warning")
        status = fake_game_status()
        status.steam_is_current = False
        panel._on_game_status(status)
        self.assertIn("Steam has an update ready", panel.message.text())
        self.assertEqual(panel.message.property("role"), "warning")
        self.assertTrue(panel.pin.isVisibleTo(panel))
        self.assertEqual(panel.steam_chip.text(), "Update waiting")
        panel._on_game_status(fake_game_status())
        self.assertNotIn("Steam has an update ready", panel.message.text())
        self.assertEqual(panel.message.property("role"), "note")

    def test_skse_warning_is_short_and_keeps_technical_details_aside(self):
        service, panel = self._panel()
        version = gameversion.GameVersion.parse
        status = fake_game_status()
        status.installed = version("1.6.1170")
        status.skse_runtime = version("1.5.97")
        status.recipe_targets = ["1.5.97"]
        status.backup_present = True
        check = gameversion.VersionCheck(status.installed, None, skse=gameversion.SkseCheck([
            gameversion.SkseFile(Path("skse64_1_5_97.dll"), status.skse_runtime, "game folder")]))
        with patch.object(service, "game_version_check", return_value=check):
            panel._on_game_status(status)
        text = panel.message.text()
        self.assertIn("SKSE needs Skyrim 1.5.97", text)
        self.assertIn("Update Skyrim in Steam first", text)
        self.assertIn("Restore original files", text)
        self.assertNotIn(".dll", text)
        self.assertNotIn("runtime", text)
        self.assertLess(len(text.split()), 50)
        self.assertIn("skse64_1_5_97.dll", panel.details)
        panel.details_tile.click()
        window = panel.window()
        self.assertIn("skse64_1_5_97.dll", window.top_overlay.text.text())

    def test_wrong_skse_offers_the_matching_build_or_the_download_page(self):
        service, panel = self._panel()
        version = gameversion.GameVersion.parse
        status = fake_game_status()
        status.installed = version("1.6.1170")
        status.expected = version("1.6.1170")
        status.skse_runtime = version("1.5.97")
        check = gameversion.VersionCheck(status.installed, status.expected)
        with patch.object(service, "game_version_check", return_value=check):
            panel._on_game_status(status)
        self.assertTrue(panel.skse.isVisibleTo(panel))
        self.assertEqual(panel.skse.text(), "Install SKSE 2.2.6")
        self.assertEqual(panel.skse.property("tileRole"), "primary")
        self.assertIn("built for Skyrim 1.5.97, not 1.6.1170", panel.message.text())
        self.assertIn("Install SKSE 2.2.6", panel.message.text())

        # The current Steam build's SKSE is Nexus-only: point there instead.
        status.installed = status.expected = version("1.7.104")
        status.skse_runtime = None
        with patch.object(service, "game_version_check", return_value=check):
            panel._on_game_status(status)
        self.assertEqual(panel.skse.text(), "Get SKSE…")
        self.assertIn("nexusmods.com", panel.message.text())
        with patch("modsync.ui.pages.game.QDesktopServices.openUrl") as open_url:
            panel._install_skse()
        self.assertIn("nexusmods.com", open_url.call_args.args[0].toString())

        status.installed = status.expected = status.skse_runtime = version("1.6.1170")
        with patch.object(service, "game_version_check", return_value=check):
            panel._on_game_status(status)
        self.assertFalse(panel.skse.isVisibleTo(panel))

    def test_unsupported_target_and_active_update_do_not_suggest_updating_to_downgrade(self):
        service, panel = self._panel()
        version = gameversion.GameVersion.parse
        status = fake_game_status()
        status.installed = version("1.6.1170")
        status.expected = version("1.5.97")
        check = gameversion.VersionCheck(status.installed, status.expected)
        with patch.object(service, "game_version_check", return_value=check):
            panel._on_game_status(status)
            self.assertIn("can't switch this install to 1.5.97 yet", panel.message.text())
            self.assertNotIn("Update Skyrim in Steam first", panel.message.text())
            status.steam_updating = True
            panel._on_game_status(status)
            self.assertIn("Wait for it to finish", panel.message.text())
            self.assertNotIn("Some mods may not work", panel.message.text())
            self.assertNotIn("return here to switch", panel.message.text())
            self.assertEqual(panel.steam_chip.text(), "Steam updating")


class FirewallTests(UiTestCase):
    def test_firewall_tile_toggles_between_allow_and_remove(self):
        from modsync.firewall import Check, Firewall

        State(instance_path=str(self.tmp)).save()
        blocked = Check(Firewall("ufw"), False, "ufw:1")
        with patch("modsync.ui.pages.system.firewall.check", return_value=blocked):
            window = self.window()
        system = window.pages["system"]
        tile = system.fw_tile
        self.assertTrue(tile.isVisibleTo(system))
        self.assertEqual(tile.text(), "Allow in firewall…")
        self.assertEqual(tile.badge.text(), "Blocked")
        self.assertIn("password", tile.description)
        system.details_tile.click()
        self.assertIn("21029/tcp", window.top_overlay.text.text())
        window.top_overlay.cancel()
        self.assertIs(window.firewall_check, blocked)

        # Allow: the stamp is remembered and the rules are re-read, not assumed.
        allowed = Check(Firewall("ufw"), True, "ufw:2")
        with patch("modsync.ui.pages.system.firewall.allow", return_value="ufw:2"), \
                patch("modsync.ui.pages.system.firewall.check", return_value=allowed):
            tile.click()
            self.settle(4)
        self.assertEqual(State.load().firewall_rules_stamp, "ufw:2")
        self.assertEqual(tile.text(), "Remove firewall rules…")
        self.assertEqual(tile.badge.text(), "Allowed")
        self.assertTrue(window.firewall_check.allowed)

        # Remove: stamp cleared, and whatever the rules say now is shown.
        with patch("modsync.ui.pages.system.firewall.revoke", return_value="ufw:3") as revoke, \
                patch("modsync.ui.pages.system.firewall.check", return_value=blocked):
            tile.click()
            self.settle(4)
        revoke.assert_called_once()
        self.assertEqual(State.load().firewall_rules_stamp, "")
        self.assertEqual(tile.text(), "Allow in firewall…")

        # A failure shows the manual commands in a sheet instead of a dialog.
        with patch("modsync.ui.pages.system.firewall.allow", side_effect=RuntimeError("pkexec failed")):
            tile.click()
            self.settle(4)
        self.assertIn("ufw allow", window.top_overlay.text.text())

        system.on_firewall_checked(Check(None, True, ""))
        self.assertFalse(tile.isVisibleTo(system))


class SteamLaunchTests(UiTestCase):
    """Steam's launch hook opens the regular window with the launch waiting on it."""

    def _window(self, through=None):
        return self.window(steam=launchhook.SteamLaunch(SKYRIM_SE, through))

    def test_play_becomes_continue_and_names_the_next_step(self):
        window = self._window(through="mo2_489830_redirector")
        home = window.pages["home"]
        self.assertEqual(home.play.text(), "Continue to Mod Organizer")
        self.assertTrue(home.play.isEnabled())  # no instance needed to go on
        self.assertTrue(home.cancel_launch.isEnabled())
        self.assertIn("Mod Organizer 2 (MO2-LINT)", home.steam_note.text())
        self.assertTrue(home.hook_tile.isHidden())  # Steam's Play button already led here
        other = self._window(through="GE-Proton10-34")
        self.assertEqual(other.pages["home"].play.text(), "Continue to Skyrim Special Edition")

    def test_enter_or_a_continues_from_the_focused_continue(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from modsync.ui.input import InputRouter

        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            with self.subTest(key=key):
                window = self._window(through="mo2_489830_redirector")
                self.assertIs(self.app.focusWidget(), window.pages["home"].play)
                QTest.keyClick(self.app.focusWidget(), key)
                self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        window = self._window()
        time.sleep(0.2)  # a separate press, not Steam's key echo of the last one
        with patch.object(InputRouter, "_app_active", staticmethod(lambda: True)):
            InputRouter.instance().pad_button("a", True)
            InputRouter.instance().pad_button("a", False)
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)

    def test_moving_to_cancel_launch_and_choosing_it_cancels(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window = self._window()
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
        self.assertIs(self.app.focusWidget(), window.pages["home"].cancel_launch)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)
        self.assertFalse(window.isVisible())

    def test_launched_from_the_desktop_nothing_changes(self):
        window = self.window()
        self.assertIsNone(window.pages["home"].cancel_launch)
        window.decide_launch(launchhook.EXIT_CONTINUE)  # no Steam launch to decide
        self.assertTrue(window.isVisible())

    def test_play_and_open_mo2_are_handed_to_steam(self):
        State(instance_path=str(self.tmp)).save()
        for play in (True, False):
            with self.subTest(play=play):
                plan = handoff_plan(self.tmp, play)
                with patch.object(ModSyncService, "launch_mo2") as launch, \
                        patch.object(ModSyncService, "prepare_mo2", return_value=(plan, "")) as prepare:
                    window = self._window(through="GE-Proton10-34")
                    home = window.pages["home"]
                    self.assertEqual(home.play.text(), "Play Skyrim")  # the same tiles as from the desktop
                    self.assertIn("cloud saves", home.steam_note.text())
                    self.assertIsNotNone(window.pages["mods"].open_mo2)
                    (home.play if play else window.pages["mods"].open_mo2).click()
                    self.settle()
                    prepare.assert_called_once_with(play=play)
                    launch.assert_not_called()  # ModSync starts nothing itself: the hook runs it
                self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
                self.assertIs(window.steam_launch.plan, plan)
                self.assertFalse(window.isVisible())

    def test_a_launch_that_cannot_be_prepared_keeps_steam_waiting(self):
        State(instance_path=str(self.tmp)).save()
        with patch.object(ModSyncService, "prepare_mo2", side_effect=RuntimeError("Choose an MO2 instance first.")):
            window = self._window()
            window.pages["home"].play.click()
            self.settle()
        self.assertIsNone(window.steam_launch.decision)
        self.assertIsNone(window.steam_launch.plan)
        self.assertTrue(window.isVisible())
        self.assertFalse(window.busy)
        self.assertIn("⚠ Choose an MO2 instance first.", window.messages)
        window.pages["home"].cancel_launch.click()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_setup_summary_and_decisions(self):
        inst = self.tmp / "MO2"
        (inst / "profiles" / "Default").mkdir(parents=True)
        (inst / "mods").mkdir()
        (inst / "ModOrganizer.ini").write_text(
            "[General]\ngameName=Skyrim Special Edition\nselected_profile=@ByteArray(Default)\n")
        (inst / "profiles/Default/modlist.txt").write_text("+SkyUI\n-Unused\n+USSEP\n")
        State(instance_path=str(inst), instance_label="My setup").save()
        window = self._window()
        self.assertIn("Profile: Default  ·  2 mods enabled", window.pages["mods"].setup_label.text())
        self.assertIn("2 mods enabled", window.pages["home"].mo2_row.description)
        with patch.object(ModSyncService, "prepare_mo2", return_value=(handoff_plan(inst), "")):
            window.pages["home"].play.click()
            self.settle()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        window.decide_launch(launchhook.EXIT_CANCEL)  # a second decision does not overwrite the first
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        other = self._window()
        other.pages["home"].cancel_launch.click()
        self.assertEqual(other.steam_launch.decision, launchhook.EXIT_CANCEL)
        closed = self._window()
        closed.close()
        self.assertEqual(closed.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_decision_survives_a_trip_through_the_wizard(self):
        from modsync.ui.input import Action

        window = self._window(through="mo2_489830_redirector")
        self.assertIsNotNone(window.start_setup())
        window.setup.handle_action(Action.BACK)
        self.settle()
        self.assertIsNone(window.setup)
        self.assertEqual(window.pages["home"].play.text(), "Continue to Mod Organizer")
        self.assertIsNone(window.steam_launch.decision)
        window.start_setup()
        window.close()  # closing from the wizard cancels too
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_unfinished_sync_is_flagged_next_to_continue(self):
        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        window = self._window()
        home, sync = window.pages["home"], window.pages["sync"]
        self.assertFalse(home.sync_warning.isVisibleTo(home))  # fake status: idle, 100%
        sync.on_status(SyncStatus("ME", "modsync-1", True, "syncing", 64.0, []))
        self.assertTrue(home.sync_warning.isVisibleTo(home))
        self.assertIn("64%", home.sync_warning.text())
        sync.on_status(SyncStatus("ME", "modsync-1", True, "idle", 100.0, []))
        self.assertFalse(home.sync_warning.isVisibleTo(home))

    def test_usvfs_fix_applies_and_restores_while_steam_waits(self):
        from modsync.mo2 import usvfs

        State(instance_path=str(self.tmp)).save()
        current = usvfs.Status("available", "ARM64 fix available", True)

        def apply(self):
            nonlocal current
            current = usvfs.Status("patched", "ARM64 fix installed", can_restore=True)
            return "applied"

        def restore(self):
            nonlocal current
            current = usvfs.Status("available", "ARM64 fix available", True)
            return "restored"

        with patch.object(usvfs, "is_arm64", return_value=True), \
                patch.object(ModSyncService, "usvfs_status", lambda self: current), \
                patch.object(ModSyncService, "apply_usvfs_fix", apply), \
                patch.object(ModSyncService, "restore_usvfs", restore):
            window = self._window()
            mods = window.pages["mods"]
            self.settle()
            self.assertTrue(mods.usvfs_apply.isVisibleTo(mods))
            mods.usvfs_apply.click()
            self.settle(6)
            self.assertTrue(mods.usvfs_restore.isVisibleTo(mods))
            mods.usvfs_restore.click()
            self.settle(6)
            self.assertIn("applied", window.messages)
            self.assertIn("restored", window.messages)
            self.assertTrue(mods.usvfs_apply.isVisibleTo(mods))
            self.assertTrue(window.pages["home"].play.isEnabled())
            self.assertIsNone(window.steam_launch.decision)

    def test_auto_decision_env_is_a_testing_aid(self):
        from PySide6.QtTest import QTest

        with patch.dict(os.environ, {"MODSYNC_HUB_AUTO_DECISION": "cancel"}):
            window = self._window()
        self.assertIn("Test mode", window.messages[0])
        QTest.qWait(3500)
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)


class BusyTests(UiTestCase):
    def test_usvfs_replacement_blocks_launch_and_navigation(self):
        from modsync.mo2 import usvfs

        State(instance_path=str(self.tmp)).save()
        for steam in (False, True):
            with self.subTest(steam_launch=steam):
                release = Event()
                available = usvfs.Status("available", "ARM64 fix available", True)
                with patch.object(usvfs, "is_arm64", return_value=True), \
                        patch.object(ModSyncService, "usvfs_status", return_value=available), \
                        patch.object(ModSyncService, "apply_usvfs_fix", side_effect=lambda: release.wait(5)), \
                        patch.object(ModSyncService, "launch_mo2") as launch:
                    window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE) if steam else None)
                    mods, home = window.pages["mods"], window.pages["home"]
                    try:
                        mods.usvfs_apply.click()
                        self.assertTrue(mods.usvfs_busy)
                        self.assertTrue(window.busy)
                        self.assertFalse(home.play.isEnabled())
                        self.assertFalse(window.pages["system"].wizard_tile.isEnabled())
                        self.assertFalse(window.pages["game"].panel.isEnabled())
                        self.assertIsNone(window.start_setup())
                        self.assertFalse(window.close())
                        if steam:
                            self.assertFalse(home.cancel_launch.isEnabled())
                            window.decide_launch(launchhook.EXIT_CONTINUE)
                            window.decide_launch(launchhook.EXIT_CANCEL)
                            self.assertIsNone(window.steam_launch.decision)
                        else:
                            window.launch(play=True)
                            launch.assert_not_called()
                    finally:
                        release.set()
                        self.settle(4)
                    self.assertFalse(window.busy)
                    self.assertTrue(window.pages["game"].panel.isEnabled())
                    window.close()
                if steam:
                    self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_file_changes_block_navigation_and_close(self):
        for operation in ("downgrade", "restore"):
            for fail in (False, True):
                with self.subTest(operation=operation, fail=fail):
                    window = self.window()
                    panel = window.pages["game"].panel
                    panel.game.expected = gameversion.GameVersion.parse("1.6.1170")
                    release = Event()

                    def work(*args):
                        if not release.wait(5):
                            raise RuntimeError("test operation timed out")
                        if fail:
                            raise RuntimeError("test failure")
                        return object()

                    method = "run_downgrade" if operation == "downgrade" else "restore_game_files"
                    with patch.object(window.service, method, side_effect=work):
                        try:
                            if operation == "downgrade":
                                panel._start_downgrade()
                            else:
                                panel._restore_files()
                            window.top_overlay.tiles["go"].click()  # the confirmation sheet, as A would
                            self.assertTrue(panel.busy)
                            self.assertFalse(window.pages["system"].wizard_tile.isEnabled())
                            self.assertFalse(window.pages["mods"].content.isEnabled())
                            self.assertFalse(window.pages["sync"].content.isEnabled())
                            self.assertIsNone(window.start_setup())
                            pages = dict(window.pages)
                            window.rebuild()  # e.g. a sync change landing now
                            self.assertEqual(window.pages, pages)  # nothing torn down underneath
                            self.assertFalse(window.close())
                            # A status response already in flight must not re-enable actions.
                            panel._on_game_status(panel.game)
                            self.assertFalse(panel.refresh_tile.isEnabled())
                            self.assertTrue(panel.restore.isHidden())
                            self.assertTrue(panel.downgrade.isHidden())
                            self.assertTrue(panel.progress.isVisibleTo(panel))
                        finally:
                            release.set()
                            self.settle(4)
                    self.assertFalse(panel.busy)
                    self.assertIsNot(window.pages["game"], pages["game"])  # the deferred rebuild ran
                    panel = window.pages["game"].panel
                    self.assertTrue(window.pages["system"].wizard_tile.isEnabled())
                    self.assertTrue(window.pages["mods"].content.isEnabled())
                    self.assertTrue(panel.refresh_tile.isEnabled())
                    if fail:
                        self.assertIn("test failure", window.last_message)
                    self.assertTrue(window.close())

    def test_cancelling_a_confirmation_changes_nothing(self):
        window = self.window()
        panel = window.pages["game"].panel
        panel.game.expected = gameversion.GameVersion.parse("1.6.1170")
        with patch.object(window.service, "run_downgrade") as run:
            panel._start_downgrade()
            focus_before = window.top_overlay.restore_focus
            from modsync.ui.input import Action, InputRouter

            InputRouter.instance().dispatch(Action.BACK)
            self.settle()
        run.assert_not_called()
        self.assertIsNone(window.top_overlay)
        self.assertFalse(panel.busy)
        self.assertIs(window.focusWidget(), focus_before)

    def test_install_blocks_window_close_and_recovers_on_failure(self):
        window = self.window()
        setup = window.start_setup()
        sheet = setup.chooser.open_install()
        release = Event()

        def install(*args, **kwargs):
            if not release.wait(5):
                raise RuntimeError("test operation timed out")
            raise RuntimeError("installer failed")

        with patch.object(sheet._installer, "available", return_value=(True, "")), \
                patch.object(sheet._installer, "install", side_effect=install):
            try:
                sheet.start()
                self.assertTrue(window.busy)
                sheet.cancel()  # B does nothing while installing
                self.assertIs(window.top_overlay, sheet)
                self.assertFalse(window.close())
                self.assertFalse(sheet.dest_tile.isEnabled())
            finally:
                release.set()
                self.settle(4)
        self.assertFalse(window.busy)
        self.assertTrue(sheet.dest_tile.isEnabled())
        self.assertEqual(sheet.run.text(), "Try again")
        self.assertIn("installer failed", sheet.log.toPlainText())
        self.assertTrue(window.close())

    def test_steam_launch_cannot_continue_or_close_during_restore(self):
        window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE))
        home = window.pages["home"]
        release = Event()
        with patch.object(window.service, "restore_game_files", side_effect=lambda: release.wait(5)):
            try:
                window.pages["game"].panel._restore_files(confirm=False)
                self.assertFalse(home.play.isEnabled())
                self.assertFalse(home.cancel_launch.isEnabled())
                window.decide_launch(launchhook.EXIT_CONTINUE)
                window.decide_launch(launchhook.EXIT_CANCEL)
                self.assertFalse(window.close())
                self.assertIsNone(window.steam_launch.decision)
            finally:
                release.set()
                self.settle(4)
        self.assertTrue(home.play.isEnabled())
        home.cancel_launch.click()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)


class SetupFlowTests(UiTestCase):
    def test_not_now_finishes_with_instance_remembered(self):
        window = self.window()
        window.pages["home"].setup.click()
        setup = window.setup
        self.assertEqual(setup.index, 0)
        self.assertEqual(setup.head.eyebrow.text(), "SETUP  ·  STEP 1 OF 3")
        self.assertFalse(window.topbar.lb.isVisible())  # sections are out of the way
        setup.chooser.use(str(self.tmp))
        self.settle()
        self.assertEqual(setup.index, 1)
        self.assertEqual(State.load().instance_path, str(self.tmp))
        self.assertFalse(State.load().syncing)
        setup.local.click()
        self.assertEqual(setup.index, 2)
        self.assertIs(window.focusWidget(), setup.finish_tile)
        setup.finish_tile.click()
        self.settle()
        self.assertIsNone(window.setup)
        self.assertEqual(window._current, "home")
        self.assertEqual(window.pages["home"].play.text(), "Play Skyrim")  # rebuilt with the instance

    def test_copying_from_another_machine_finishes_at_the_sync_step(self):
        """A joiner can't know which game version its mods need until they've
        synced, so the wizard must not route it through the game-version step."""
        from modsync.pairing_code import PairingCode

        window = self.window()
        setup = window.start_setup()
        setup.chooser.use(str(self.tmp))
        self.settle()
        setup.copy.click()
        self.assertTrue(setup.join_panel.isVisibleTo(setup))
        code = PairingCode("A" * 56, "modsync-abc", "Deck").encode()
        with patch.object(ModSyncService, "join_vault") as join:
            setup.join.join_code(code)
            self.settle()
        join.assert_called_once()
        self.assertIsNone(window.setup)  # never showed the game-version step

    def test_cannot_switch_instance_while_syncing(self):
        State(instance_path="/elsewhere", folder_id="modsync-1").save()
        window = self.window()
        setup = window.start_setup()
        self.assertIsNotNone(setup.keep)
        setup.chooser.use(str(self.tmp))
        self.settle()
        self.assertEqual(setup.index, 0)  # stayed put
        self.assertIn("stop syncing", window.last_message)
        setup.keep.click()
        self.assertEqual(setup.index, 1)
        self.assertTrue(setup.keep_sync.isVisibleTo(setup))

    def test_b_steps_back_and_leaves_from_the_first_step(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window = self.window()
        setup = window.start_setup()
        setup.go_to(2)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
        self.assertEqual(setup.index, 1)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
        self.settle()
        self.assertIsNone(window.setup)


class LaunchTests(UiTestCase):
    def test_launch_tiles_route_actions_and_recover_after_failure(self):
        State(instance_path="/example/MO2").save()
        window = self.window()
        home, mods = window.pages["home"], window.pages["mods"]
        with patch("modsync.ui.main_window.worker.run_async") as run:
            home.play.click()
            self.assertTrue(run.call_args.kwargs["play"])
            self.assertTrue(window.busy)
            self.assertFalse(home.open_mo2.isEnabled())
            self.assertFalse(window.pages["game"].panel.isEnabled())
            window._on_launch_failed("Start Steam first")
            self.assertFalse(window.busy)
            self.assertTrue(home.play.isEnabled())
            self.assertIn("Start Steam first", window.last_message)
            mods.open_mo2.click()
            self.assertFalse(run.call_args.kwargs["play"])
            window._on_launched("Starting MO2")
        with patch.object(window.service.launcher, "running", return_value=True):
            window.poll()
            self.assertFalse(home.play.isEnabled())
            self.assertFalse(home.open_mo2.isEnabled())
            self.assertFalse(window.pages["game"].panel.isEnabled())
        window.poll()
        self.assertTrue(home.play.isEnabled())
        self.assertTrue(window.pages["game"].panel.isEnabled())

    def test_play_requires_chosen_instance(self):
        window = self.window()
        self.assertFalse(window.pages["home"].play.isEnabled())
        self.assertIsNone(window.pages["mods"].open_mo2)


class QuitTests(UiTestCase):
    def test_b_on_home_asks_then_quits(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window = self.window()
        self.assertIn("Quit", window.hintbar.texts)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)  # B
        sheet = window.top_overlay
        self.assertEqual(sheet.title.text(), "Quit ModSync?")
        self.assertIs(self.app.focusWidget(), sheet.tiles["quit"])
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)  # B again: stay
        self.assertIsNone(window.top_overlay)
        self.assertTrue(window.isVisible())
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)  # A on Quit
        self.assertFalse(window.isVisible())

    def test_b_elsewhere_still_goes_home_first(self):
        from modsync.ui.input import Action, InputRouter

        window = self.window()
        window.go("system")
        InputRouter.instance().dispatch(Action.BACK)
        self.assertEqual(window._current, "home")
        self.assertIsNone(window.top_overlay)

    def test_quit_from_system_and_ctrl_q(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window = self.window()
        window.pages["system"].quit_tile.click()
        self.assertEqual(window.top_overlay.title.text(), "Quit ModSync?")
        window.pages["system"].quit_tile.click()  # never stacks a second question
        self.assertEqual(len(window.overlays), 1)
        window.top_overlay.cancel()
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
        window.top_overlay.tiles["quit"].click()
        self.assertFalse(window.isVisible())

    def test_quitting_while_steam_waits_cancels_the_launch(self):
        window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE))
        window.request_quit()
        sheet = window.top_overlay
        self.assertIn("without starting anything", sheet.text.text())
        self.assertEqual(sheet.tiles["quit"].text(), "Quit and return to Steam")
        sheet.tiles["quit"].click()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_quit_waits_for_file_changes(self):
        window = self.window()
        window.set_busy("game", True)
        window.request_quit()
        self.assertIsNone(window.top_overlay)
        self.assertIn("still working", window.last_message)
        self.assertTrue(window.isVisible())


class SetupFinishTests(UiTestCase):
    def test_finishing_with_a_version_mismatch_is_explicit(self):
        version = gameversion.GameVersion.parse
        status = fake_game_status()
        status.installed = version("1.6.1170")
        status.expected = version("1.5.97")
        status.recipe_from = "1.6.1170"
        status.recipe_targets = ["1.5.97"]
        check = gameversion.VersionCheck(status.installed, status.expected)
        with patch.object(ModSyncService, "game_status", lambda self, refresh_index=True: status), \
                patch.object(ModSyncService, "game_version_check", lambda self: check):
            window = self.window()
            setup = window.start_setup()
            setup.go_to(2)
            self.settle()
        self.assertEqual(setup.finish_tile.text(), "Finish anyway")
        self.assertEqual(setup.finish_tile.property("tileRole"), "normal")
        self.assertIn("need 1.5.97", setup.finish_tile.description)
        self.assertIs(self.app.focusWidget(), setup.game.downgrade)  # the repair, not Finish

    def test_finish_stays_the_main_action_when_nothing_needs_fixing(self):
        window = self.window()
        setup = window.start_setup()
        setup.go_to(2)
        self.settle()
        self.assertEqual(setup.finish_tile.text(), "Finish")
        self.assertEqual(setup.finish_tile.property("tileRole"), "primary")
        self.assertIs(self.app.focusWidget(), setup.finish_tile)


class HookUpgradeTests(UiTestCase):
    def test_home_brings_an_old_hook_up_to_date_before_reading_it(self):
        calls = []

        def upgrade(appid=SKYRIM_SE.appid):
            calls.append("upgrade")
            return "Renamed the launch hook so Steam Cloud syncs Skyrim's saves again."

        def status(appid=SKYRIM_SE.appid):
            calls.append("status")
            return launchhook.LaunchHookStatus(SKYRIM_SE, False, None, False, None, None, False, None, False, True)

        with patch.object(launchhook, "upgrade", upgrade), patch.object(launchhook, "status", status):
            window = self.window()
            self.settle(4)
        self.assertEqual(calls[:2], ["upgrade", "status"])
        self.assertIn("Steam Cloud", window.last_message)
