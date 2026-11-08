"""Cyberpunk 2077 as a game Home can show, with modsync.cyberpunk faked."""

from pathlib import Path
from unittest.mock import patch

from modsync import cyberpunk
from modsync.heroic.hook import HookStatus
from modsync.heroic.library import GogGame, WineVersion
from modsync.state import State
from modsync.ui.input import Action, InputRouter
from modsync.ui.overlays import FolderSheet, GameSheet
from tests.ui_support import UiTestCase

INSTANCE = Path("/games/ModOrganizer2-cyberpunk2077")


def overview(*, heroic=True, prefix=True, instance=True, chosen=True, hook_on=False, mods="off",
             instance_problem=None, version_problem=None):
    game = None
    if heroic:
        game = GogGame("1423049311", Path("/games/Heroic/Cyberpunk 2077"), "2.5_RedModFix", "1", True,
                       Path("/heroic"), prefix=Path("/games/Heroic/Prefixes/Cyberpunk 2077"),
                       wine=WineVersion(Path("/proton/proton"), "proton", "proton"))
    hook = HookStatus(hook_on, hook_on, Path("/heroic/modsync/play-1423049311.sh")) if heroic else None
    return cyberpunk.Overview(game, INSTANCE if instance else None, instance and chosen, instance_problem, hook,
                              mods, ("2.5", "3.0.88.46235") if heroic else None, version_problem), prefix


class CyberpunkUiTests(UiTestCase):
    def open_sheet(self, ov_prefix):
        """Home showing Cyberpunk, with ``ov`` as what's set up for the rest of the test."""
        ov, prefix = ov_prefix
        for p in (patch.object(cyberpunk, "overview", lambda hook=True: ov),
                  patch.object(GogGame, "drive_c", property(lambda self: Path("/c") if prefix else None))):
            p.start()
            self.addCleanup(p.stop)
        window = self.window()
        window.switch_game("cyberpunk")
        self.settle(3)
        return window, window.pages["cyberpunk"]

    def test_the_picker_offers_cyberpunk_before_heroic_has_it(self):
        window = self.window()  # the test machine has no Heroic
        self.settle(3)
        InputRouter.instance().dispatch(Action.AUX)
        self.settle(2)
        sheet = window.top_overlay
        self.assertIsInstance(sheet, GameSheet)
        row = sheet.rows["cyberpunk"]
        self.assertEqual(row.badge.text(), "Not installed")
        self.assertIn("Heroic Games Launcher", row.description)

    def test_reopening_on_cyberpunk_that_isnt_here_opens_on_skyrim(self):
        State.update(home_game="cyberpunk")
        window = self.window()  # no Heroic here
        self.settle(3)
        self.assertIs(window.page, window.pages["home"])
        self.assertEqual(State.load().home_game, "cyberpunk")  # kept for once it's here

    def test_home_reopens_on_cyberpunk_when_heroic_has_it(self):
        State.update(home_game="cyberpunk")
        ov = overview()
        with patch.object(cyberpunk, "overview", lambda hook=True: ov[0]), \
                patch.object(GogGame, "drive_c", property(lambda self: Path("/c"))):
            window = self.window()
            self.settle(3)
        self.assertIs(window.page, window.pages["cyberpunk"])

    def test_without_heroics_cyberpunk_it_says_where_to_get_it(self):
        window, page = self.open_sheet(overview(heroic=False, instance=False))
        self.assertEqual(page.game_row.badge.text(), "Not installed")
        self.assertFalse(page.install_tile.isVisibleTo(page))
        self.assertFalse(page.play.isVisibleTo(page))
        for row in (page.hook_row, page.mods_row):
            self.assertFalse(row.isEnabled())

    def test_a_prefix_heroic_hasnt_made_asks_for_one_launch(self):
        window, page = self.open_sheet(overview(prefix=False, instance=False))
        self.assertEqual(page.game_row.badge.text(), "Not set up")
        self.assertIn("Launch it once from Heroic", page.game_row.description)
        self.assertFalse(page.install_tile.isEnabled() and page.install_tile.isVisibleTo(page))

    def test_before_mo2_the_blue_action_installs_it(self):
        window, page = self.open_sheet(overview(instance=False))
        self.assertEqual(page.game_row.badge.text(), "2.5")
        self.assertTrue(page.install_tile.isVisibleTo(page))
        self.assertIs(page.preferred_focus(), page.install_tile)

        def install(dest, on_output):
            on_output("2026-11-08 05:00:00 | INFO | INSTALLER | noise")  # MO2-LINT's own lines stay out
            on_output("Applying xaudio2_7=native to Cyberpunk's prefix…")
            return INSTANCE

        with patch.object(cyberpunk, "install_mo2", side_effect=install) as installed:
            page.install_tile.click()
            self.settle(4)
        installed.assert_called_once()
        self.assertEqual(installed.call_args.args[0], cyberpunk.default_instance_dir())
        self.assertIn("Mod Organizer 2 is installed", window.messages[-1])
        self.assertFalse(window.busy)

    def test_play_and_open_go_through_mo2(self):
        window, page = self.open_sheet(overview())
        self.assertIs(page.preferred_focus(), page.play)
        self.assertTrue(page.open_mo2.isVisibleTo(page))
        with patch.object(cyberpunk, "launch", return_value="Starting Cyberpunk 2077 through MO2…") as launch:
            page.play.click()
            self.settle(4)
            page.open_mo2.click()
            self.settle(4)
        self.assertEqual([c.kwargs["play"] for c in launch.call_args_list], [True, False])
        self.assertIs(launch.call_args.args[0], window.service.launcher)

    def test_rows_switch_heroics_play_button_and_sync(self):
        window, page = self.open_sheet(overview())
        self.assertEqual(page.hook_row.badge.text(), "Game only")
        with patch.object(cyberpunk, "enable_hook", return_value=["Heroic's Play button goes through MO2."]) as on:
            page.hook_row.click()
            self.settle(4)
        on.assert_called_once()
        self.assertIn("Heroic's Play button", window.messages[-1])
        with patch.object(window.service, "share_game_folder", return_value=INSTANCE) as share:
            page.mods_row.click()
            self.settle(4)
        share.assert_called_once_with("cp2077-mo2")
        self.assertIn("mods now sync", window.messages[-1])

    def test_turning_things_off_again(self):
        window, page = self.open_sheet(overview(hook_on=True, mods="on"))
        self.assertEqual(page.hook_row.badge.text(), "Mod Organizer 2")
        self.assertEqual(page.mods_row.badge.text(), "On")
        with patch.object(cyberpunk, "disable_hook", return_value="Starts it as before.") as off:
            page.hook_row.click()
            self.settle(4)
        off.assert_called_once()
        with patch.object(window.service, "stop_game_folder", return_value=True) as stop:
            page.mods_row.click()
            self.settle(4)
        stop.assert_called_once_with("cp2077-mo2")
        self.assertIn("Nothing was deleted", window.messages[-1])

    def test_syncing_a_found_instance_hands_it_over_first(self):
        """One MO2-LINT installed is played but only synced once chosen: the row chooses it."""
        window, page = self.open_sheet(overview(chosen=False))
        self.assertIn("use this instance here", page.mods_row.description)
        with patch.object(cyberpunk, "use_instance") as use, \
                patch.object(window.service, "share_game_folder", return_value=INSTANCE):
            page.mods_row.click()
            self.settle(4)
        use.assert_called_once_with(INSTANCE)

    def test_another_instance_can_be_chosen(self):
        window, page = self.open_sheet(overview())
        self.assertEqual(page.use_existing.text(), "Use another installation…")
        page.mo2_row.click()
        self.settle(2)
        self.assertIsInstance(window.top_overlay, FolderSheet)
        window.top_overlay.close()
        self.settle()
        with patch.object(cyberpunk, "use_instance", return_value=Path("/other")) as use, \
                patch.object(cyberpunk, "refresh_hook", return_value=["Updated Heroic's Play hook."]):
            page._use(Path("/other"))
            self.settle(4)
        use.assert_called_once_with(Path("/other"))
        self.assertEqual(window.messages[-1], "Using Mod Organizer 2 in /other. Updated Heroic's Play hook.")

    def test_a_version_mismatch_is_said_plainly_and_in_the_picker(self):
        problem = "Cyberpunk 2077 here is 2.5 (build 3.0.88.46235), but the synced mods were set up for 2.3."
        ov = overview(mods="on", version_problem=problem)
        window, page = self.open_sheet(ov)
        self.assertTrue(page.note.isVisibleTo(page))
        self.assertEqual(page.note.text(), problem)
        self.assertEqual(page.summary(ov[0]), ("Needs a look", "warn", problem))

    def test_a_failure_is_shown_and_the_window_is_free_again(self):
        window, page = self.open_sheet(overview())
        with patch.object(cyberpunk, "enable_hook", side_effect=cyberpunk.CyberpunkError("Close Heroic first.")):
            page.hook_row.click()
            self.settle(4)
        self.assertEqual(window.messages[-1], "⚠ Close Heroic first.")
        self.assertFalse(window.busy)
        self.assertTrue(page.play.isEnabled())
