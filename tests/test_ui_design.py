"""Action priorities and text editing in the controller design follow-up."""

from unittest.mock import patch

from modsync import gameversion, launchhook
from modsync.firewall import Check, Firewall
from modsync.games import SKYRIM_SE
from modsync.service import ModSyncService
from modsync.state import State
from tests.ui_support import UiTestCase, fake_game_status, handoff_plan


class HomeGuidanceTests(UiTestCase):
    def mismatch(self):
        status = fake_game_status()
        status.installed = gameversion.GameVersion.parse("1.6.1170")
        status.expected = gameversion.GameVersion.parse("1.5.97")
        status.recipe_from = "1.6.1170"
        status.recipe_targets = ["1.5.97"]
        return status, gameversion.VersionCheck(status.installed, status.expected)

    def test_a_mismatch_recommends_a_repair_and_still_allows_play(self):
        State(instance_path=str(self.tmp)).save()
        status, check = self.mismatch()
        with patch.object(ModSyncService, "game_status", return_value=status), \
                patch.object(ModSyncService, "game_version_check", return_value=check):
            window = self.window()
        home = window.pages["home"]
        self.assertIs(window.focusWidget(), home.next_step)
        self.assertEqual(home.next_step.text(), "Downgrade to 1.5.97…")
        self.assertIn("need Skyrim 1.5.97", home.next_step.description)
        self.assertTrue(home.play.isEnabled())
        self.assertEqual(home.play.property("tileRole"), "normal")
        self.assertIn("may not load", home.play.description)
        home.next_step.click()
        self.assertEqual(window._current, "home")
        self.assertEqual(window.top_overlay.title.text(), "Downgrade Skyrim Special Edition to 1.5.97")
        window.top_overlay.cancel()

    def test_polling_preserves_a_deliberate_choice_to_play_and_recovers_after_repair(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        status, check = self.mismatch()
        home.on_game_checked(status, check)
        home.play.setFocus()
        home.on_game_checked(status, check)
        self.assertIs(window.focusWidget(), home.play)
        home.next_step.setFocus()
        status.installed = status.skse_runtime = status.expected
        home.on_game_checked(status, gameversion.VersionCheck(status.installed, status.expected))
        self.assertTrue(home.next_step.isHidden())
        self.assertEqual(home.play.property("tileRole"), "primary")
        self.assertIs(window.focusWidget(), home.play)

    def test_guidance_does_not_take_focus_from_a_sheet_or_a_mouse_user(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        status, check = self.mismatch()
        sheet = window.confirm("Question", "Text", [("stay", "Stay", "", "normal", None)], lambda k: None)
        home.on_game_checked(status, check)
        self.assertIs(window.focusWidget(), sheet.tiles["stay"])
        sheet.cancel()
        home.on_game_checked(fake_game_status(), gameversion.VersionCheck(status.installed, None))
        window.router._set_mode("mouse")
        home.play.setFocus()
        home.on_game_checked(status, check)
        self.assertIs(window.focusWidget(), home.play)

    def test_continue_remains_an_explicit_steam_launch_choice(self):
        State(instance_path=str(self.tmp)).save()
        status, check = self.mismatch()
        steam = launchhook.SteamLaunch(SKYRIM_SE)
        with patch.object(ModSyncService, "game_status", return_value=status), \
                patch.object(ModSyncService, "game_version_check", return_value=check):
            window = self.window(steam=steam)
        self.assertIsNone(steam.decision)
        window.set_busy("game", True)
        self.assertFalse(window.pages["home"].next_step.isEnabled())
        self.assertFalse(window.pages["home"].play.isEnabled())
        window.set_busy("game", False)
        with patch.object(ModSyncService, "prepare_mo2", return_value=(handoff_plan(self.tmp), "")):
            window.pages["home"].play.click()
            self.settle()
        self.assertEqual(steam.decision, launchhook.EXIT_CONTINUE)

    def test_skse_mismatch_and_waiting_updates_have_specific_recommendations(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        status = fake_game_status()
        status.expected = status.installed
        status.skse_runtime = gameversion.GameVersion.parse("1.5.97")
        check = gameversion.VersionCheck(status.installed, status.expected)
        home.on_game_checked(status, check)
        self.assertEqual(home.next_step.text(), "Fix Script Extender (SKSE)")
        status.skse_runtime = status.installed
        status.steam_is_current = False
        home.on_game_checked(status, check)
        self.assertEqual(home.next_step.text(), "Keep this game version")
        status.pending_pin = True
        home.on_game_checked(status, check)
        self.assertTrue(home.next_step.isHidden())
        status.steam_updating = True
        status.expected = gameversion.GameVersion.parse("1.5.97")
        home.on_game_checked(status, gameversion.VersionCheck(status.installed, status.expected))
        self.assertTrue(home.next_step.isHidden())


class SetupPriorityTests(UiTestCase):
    def test_the_waiting_update_is_the_primary_action_even_if_skse_is_optional(self):
        status = fake_game_status()
        status.steam_is_current = False
        with patch.object(ModSyncService, "game_status", return_value=status):
            window = self.window()
            setup = window.start_setup()
            setup.go_to(1)
            self.settle()
        self.assertIs(window.focusWidget(), setup.game.pin)
        self.assertEqual(setup.finish_tile.text(), "Continue anyway")
        self.assertEqual(setup.game.pin.property("tileRole"), "primary")
        self.assertEqual(setup.game.skse.property("tileRole"), "normal")
        self.assertEqual(setup.finish_tile.property("tileRole"), "normal")

    def test_finish_is_the_only_primary_action_when_the_game_version_is_ready(self):
        window = self.window()
        setup = window.start_setup()
        setup.go_to(1)
        self.settle()
        primary = [t for t in [setup.finish_tile, *setup.game.action_tiles]
                   if t.isVisible() and t.property("tileRole") == "primary"]
        self.assertEqual(primary, [setup.finish_tile])


class KeyboardEditingTests(UiTestCase):
    def sheet(self, text=""):
        from modsync.ui.overlays import KeyboardSheet

        window = self.window()
        sheet = KeyboardSheet(window, "Path", "Enter a path.", lambda text: None, text=text).open()
        return window, sheet

    def test_shift_produces_punctuation_and_keeps_the_symbol_layer(self):
        _, sheet = self.sheet()
        sheet.shift.click()
        sheet.keys[0].click()
        sheet.keys[-2].click()
        self.assertEqual(sheet.field.text(), "!_")
        sheet.symbols.click()
        next(k for k in sheet.keys if k.text() == "@").click()
        self.assertEqual(sheet.field.text(), "!_@")

    def test_controller_buttons_can_insert_and_replace_selected_text(self):
        from modsync.ui.input import Action

        window, sheet = self.sheet("mods")
        sheet.cursor_left.setFocus()
        window.router.dispatch(Action.ACCEPT)
        self.assertEqual(sheet.field.cursorPosition(), 3)
        window.router.dispatch(Action.PREV_TAB)
        self.assertEqual(sheet.field.cursorPosition(), 2)
        sheet.type_text("_")
        self.assertEqual(sheet.field.text(), "mo_ds")
        sheet.select.click()
        window.router.dispatch(Action.NEXT_TAB)
        self.assertEqual(sheet.field.selectedText(), "d")
        sheet.type_text("D")
        self.assertEqual(sheet.field.text(), "mo_Ds")
        sheet.select_all_key.click()
        sheet.type_text("/home/me/mods")
        self.assertEqual(sheet.field.text(), "/home/me/mods")

    def test_physical_shortcuts_work_while_a_keyboard_button_has_focus(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        _, sheet = self.sheet("mods")
        sheet.keys[0].setFocus()
        QTest.keyClick(sheet.keys[0], Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(sheet.field.selectedText(), "mods")
        QTest.keyClicks(sheet.keys[0], "path")
        self.assertEqual(sheet.field.text(), "path")
        sheet.field.setCursorPosition(0)
        QTest.keyClick(sheet.keys[0], Qt.Key.Key_Delete)
        self.assertEqual(sheet.field.text(), "ath")

    def test_editing_hints_and_validation_fit_a_smaller_window(self):
        from PySide6.QtTest import QTest

        window, sheet = self.sheet()
        window.router._set_mode("gamepad")
        window.resize(1000, 800)
        sheet.cursor_left.setFocus()
        self.assertIn("Cursor left", window.hintbar.texts)
        sheet._validate = lambda text: "Enter an address such as 192.168.1.20 or a valid hostname."
        sheet.done()
        self.settle()
        QTest.qWait(200)
        self.assertTrue(sheet.error.isVisible())
        self.assertLessEqual(sheet.sheet.geometry().bottom(), sheet.height())
        self.assertGreaterEqual(sheet.sheet.geometry().top(), 0)
        for widget in [*sheet.keys, sheet.cursor_left, sheet.select_all_key]:
            self.assertTrue(sheet.sheet.rect().contains(widget.mapTo(sheet.sheet, widget.rect().bottomRight())))


class SystemDetailsTests(UiTestCase):
    def test_settings_details_keep_the_information_removed_from_tiles(self):
        window = self.window()
        system = window.pages["system"]
        system.on_firewall_checked(Check(Firewall("ufw"), False, "ufw:1"))
        system.details_tile.click()
        text = window.top_overlay.text.text()
        self.assertIn("starts at login", text)
        self.assertIn("21029/tcp", text)
        self.assertIn("22000/tcp", text)
        self.assertIn("password", system.fw_tile.description)
        window.top_overlay.cancel()
        system.controls_tile.click()
        self.assertEqual(window.top_overlay.title.text(), "Controls")


class FollowupReviewTests(UiTestCase):
    """Fixes made while taking over the design follow-up."""

    mismatch = HomeGuidanceTests.mismatch

    def test_a_press_made_while_the_check_runs_still_lands_on_continue(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        State(instance_path=str(self.tmp)).save()
        window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE))
        home = window.pages["home"]
        self.assertIs(window.focusWidget(), home.play)
        status, check = self.mismatch()
        QTest.keyClick(window.focusWidget(), Qt.Key.Key_Down)  # the user is already moving
        QTest.keyClick(window.focusWidget(), Qt.Key.Key_Up)
        home.on_game_checked(status, check)  # the slow check lands afterwards
        self.assertIs(window.focusWidget(), home.play)
        self.assertTrue(home.next_step.isVisibleTo(home))  # still recommended, just not forced

    def test_an_untouched_window_focuses_the_recommendation(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE))
        home = window.pages["home"]
        status, check = self.mismatch()
        home.on_game_checked(status, check)
        self.assertIs(window.focusWidget(), home.next_step)

    def test_hook_tile_explains_states_that_need_attention(self):
        window = self.window()
        system = window.pages["system"]
        missing = launchhook.LaunchHookStatus(SKYRIM_SE, False, None, False, None, None, False, None, False, False)
        system.on_hook_status(missing)
        self.assertIn("Steam was not found", system.hook_tile.description)
        self.assertFalse(system.hook_tile.isEnabled())
        broken = launchhook.LaunchHookStatus(SKYRIM_SE, True, "modsync_489830_hub", True, "proton_9", "Proton 9",
                                             False, None, False, True)
        system.on_hook_status(broken)
        self.assertIn("is missing", system.hook_tile.description)
        off = launchhook.LaunchHookStatus(SKYRIM_SE, False, None, False, None, None, False, None, False, True)
        system.on_hook_status(off)
        self.assertIn("Check your setup before Skyrim starts", system.hook_tile.description)
