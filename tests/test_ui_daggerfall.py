"""Daggerfall Unity as a game Home can show, and the switcher that picks it,
with modsync.dfu faked."""

from pathlib import Path
from unittest.mock import patch

from modsync import dfu, launchhook
from modsync.games import DAGGERFALL, SKYRIM_SE
from modsync.state import State
from modsync.ui.input import Action, InputRouter
from modsync.ui.overlays import GameSheet
from modsync.ui.widgets import HintButton
from tests.ui_support import UiTestCase


def overview(*, steam=True, installed=False, hook_on=False, mods="off", saves="off", version_problem=None,
             problems=None):
    inst = dfu.Install(Path("/games/Daggerfall Unity")) if installed else None
    hook = launchhook.LaunchHookStatus(DAGGERFALL, hook_on, "modsync_1812390_proton" if hook_on else None, hook_on,
                                       None, None, True, None, False, True,
                                       native=str(inst.executable) if hook_on and inst else None)
    dagger = Path("/steam/The Elder Scrolls Daggerfall/DF/DAGGER") if steam else None
    return dfu.Overview(dagger, problems if problems is not None else ([] if steam else ["Steam hasn't installed it."]),
                        inst, str(dagger) if installed and dagger else None, hook, mods, saves, version_problem)


class DaggerfallUiTests(UiTestCase):
    def open_sheet(self, ov):
        """Home showing Daggerfall, with ``ov`` as what's set up for the rest of the test."""
        p = patch.object(dfu, "overview", lambda hook=True: ov)
        p.start()
        self.addCleanup(p.stop)
        window = self.window()
        window.go("daggerfall")
        self.settle(3)
        return window, window.pages["daggerfall"]

    def test_the_switcher_shows_with_or_without_daggerfall(self):
        for steam in (False, True):
            with self.subTest(steam=steam), patch.object(dfu, "overview", lambda hook=True, s=steam: overview(steam=s)):
                window = self.window()
                self.settle(3)
                self.assertTrue(window.topbar.game_switch.isVisibleTo(window))
                self.assertIn("Switch game", window.hintbar.texts)
                self.assertEqual(window.topbar.game_switch.text(), "Skyrim")
                window.close()

    def test_the_picker_offers_daggerfall_before_steam_has_it(self):
        window = self.window()  # the test machine has no Daggerfall
        self.settle(3)
        InputRouter.instance().dispatch(Action.AUX)
        self.settle(2)
        sheet = window.top_overlay
        self.assertIsInstance(sheet, GameSheet)
        self.assertEqual(list(sheet.rows), ["skyrim", "daggerfall"])
        daggerfall = sheet.rows["daggerfall"]
        self.assertEqual(daggerfall.badge.text(), "Not installed")
        self.assertIn("in Steam first", daggerfall.description)
        self.assertIs(window.focusWidget(), daggerfall)
        InputRouter.instance().dispatch(Action.ACCEPT)
        self.settle(3)
        page = window.pages["daggerfall"]
        self.assertIs(window.page, page)
        self.assertIs(window.focusWidget(), page.steam_tile)

    def test_x_opens_the_picker_and_a_shows_daggerfall_on_home(self):
        with patch.object(dfu, "overview", lambda hook=True: overview(installed=True, mods="on")):
            window = self.window()
            self.settle(3)
            InputRouter.instance().dispatch(Action.AUX)
            self.settle(2)
            sheet = window.top_overlay
            self.assertIsInstance(sheet, GameSheet)
            self.assertEqual(list(sheet.rows), ["skyrim", "daggerfall"])
            self.assertEqual(sheet.rows["skyrim"].title.text(), SKYRIM_SE.name)
            self.assertTrue(sheet.rows["skyrim"].end.check)
            daggerfall = sheet.rows["daggerfall"]
            self.assertEqual(daggerfall.badge.text(), "Ready")
            self.assertIn("Syncing mods", daggerfall.description)
            # Opening it means switching: focus waits on the other game.
            self.assertIs(window.focusWidget(), daggerfall)
            self.assertIn("Switch", window.hintbar.texts)
            InputRouter.instance().dispatch(Action.ACCEPT)
            self.settle(3)
        self.assertFalse(window.overlays)
        self.assertEqual(window._current, "home")
        self.assertIs(window.page, window.pages["daggerfall"])
        self.assertTrue(window.tabs["home"].isChecked())
        self.assertEqual(window.topbar.game_switch.text(), "Daggerfall Unity")
        self.assertTrue(window.pages["daggerfall"].isAncestorOf(window.focusWidget()))
        self.assertEqual(State.load().home_game, "daggerfall")

    def test_home_opens_on_the_game_last_shown(self):
        State.update(home_game="daggerfall")
        with patch.object(dfu, "overview", lambda hook=True: overview(installed=True)):
            window = self.window()
            self.settle(3)
            self.assertIs(window.page, window.pages["daggerfall"])
            window.switch_game("skyrim")
            self.settle(3)
        self.assertIs(window.page, window.pages["home"])
        self.assertEqual(State.load().home_game, "")

    def test_daggerfall_going_away_keeps_its_home(self):
        """Uninstalled from Steam, say: Home still shows Daggerfall, now with how to get it back."""
        window, page = self.open_sheet(overview())
        window.switch_game("daggerfall")
        self.settle(3)
        page._show(overview(steam=False))
        self.settle(2)
        self.assertIs(window.page, page)
        self.assertEqual(window.topbar.game_switch.text(), "Daggerfall Unity")
        self.assertTrue(page.steam_tile.isVisibleTo(page))
        self.assertEqual(State.load().home_game, "daggerfall")

    def test_switching_with_a_lands_on_daggerfalls_blue_action(self):
        with patch.object(dfu, "overview", lambda hook=True: overview(installed=True)):
            window = self.window()
            self.settle(3)
            router = InputRouter.instance()
            router.dispatch(Action.AUX)
            self.settle()
            router.dispatch(Action.ACCEPT)
            self.settle(3)
        self.assertIs(window.focusWidget(), window.pages["daggerfall"].play)

    def test_a_late_read_never_takes_focus_from_the_picker(self):
        window, page = self.open_sheet(overview(installed=True))
        page._focus_on_load = True  # as if the first read were still on its way
        page.data_row.setFocus()
        InputRouter.instance().dispatch(Action.AUX)
        self.settle()
        sheet = window.top_overlay
        self.assertIsInstance(sheet, GameSheet)
        page._show(overview(installed=True))
        self.settle()
        self.assertTrue(sheet.isAncestorOf(window.focusWidget()))

    def test_a_late_read_never_moves_focus_after_a_press(self):
        window, page = self.open_sheet(overview(installed=True))
        page._focus_on_load = True
        page.data_row.setFocus()
        InputRouter.instance().dispatch(Action.MENU)  # Start: already Home, focus stays
        page._show(overview(installed=True))
        self.settle()
        self.assertIs(window.focusWidget(), page.data_row)

    def test_remembering_the_game_never_replaces_the_setup_in_memory(self):
        """A snapshot written on a worker could land after a newer change."""
        window, _page = self.open_sheet(overview(installed=True))
        state = window.service.state
        with patch.object(window.service, "update_state") as update_state:
            window.switch_game("skyrim")
            self.settle(3)
        update_state.assert_not_called()
        self.assertIs(window.service.state, state)
        self.assertEqual(State.load().home_game, "")

    def test_finishing_skyrims_setup_ends_on_skyrims_home(self):
        window, _page = self.open_sheet(overview(installed=True))
        window.start_setup()
        self.settle()
        window.finish_setup()
        self.settle(3)
        self.assertIs(window.page, window.pages["home"])
        self.assertEqual(window.topbar.game_switch.text(), "Skyrim")
        self.assertEqual(State.load().home_game, "")

    def test_a_deck_that_sdl_doesnt_read_is_promised_only_the_x_key(self):
        """Steam's desktop configuration turns a Deck's X into its keyboard."""
        with patch("modsync.ui.steaminput.family", return_value="deck"), \
                patch.object(dfu, "overview", lambda hook=True: overview()):
            window = self.window()
            self.settle(3)
            router = InputRouter.instance()
            self.assertFalse(router.pad_sends(Action.AUX))
            self.assertFalse(window.topbar.game_glyph.isVisibleTo(window))
            hint = next(b for b in window.hintbar.findChildren(HintButton) if b.text() == "Switch game")
            self.assertEqual([kind for kind, *_ in hint._parts()], ["key"])
            router.pad_button("x", True)  # SDL reads the Deck after all: X arrives
            router.pad_button("x", False)
            self.settle()
            self.assertTrue(router.pad_sends(Action.AUX))
            self.assertTrue(window.topbar.game_glyph.isVisibleTo(window))

    def test_steams_skyrim_launch_shows_only_skyrim(self):
        State.update(home_game="daggerfall")
        with patch.object(dfu, "overview", lambda hook=True: overview(installed=True)):
            window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE))
            self.settle(3)
            InputRouter.instance().dispatch(Action.AUX)
            self.settle(2)
        self.assertIs(window.page, window.pages["home"])
        self.assertFalse(window.overlays)
        self.assertFalse(window.topbar.game_switch.isVisibleTo(window))

    def test_left_from_the_first_tab_reaches_the_switcher(self):
        with patch.object(dfu, "overview", lambda hook=True: overview()):
            window = self.window()
            self.settle(3)
            router = InputRouter.instance()
            window.tabs["home"].setFocus()
            router.dispatch(Action.LEFT)
            self.settle()
            self.assertIs(window.focusWidget(), window.topbar.game_switch)
            router.dispatch(Action.RIGHT)
            self.settle()
            self.assertIs(window.focusWidget(), window.tabs["home"])
            window.topbar.game_switch.setFocus()
            router.dispatch(Action.ACCEPT)
            self.settle(2)
            self.assertIsInstance(window.top_overlay, GameSheet)

    def test_before_install_the_blue_action_installs(self):
        window, page = self.open_sheet(overview())
        self.assertTrue(page.install_tile.isVisibleTo(page))
        self.assertFalse(page.play.isVisibleTo(page))
        self.assertTrue(page.install_tile.isEnabled())
        for row in (page.hook_row, page.mods_row, page.saves_row):
            self.assertFalse(row.isEnabled())
        installed = dfu.Installed(dfu.Install(Path("/games/Daggerfall Unity")), Path("/d"), fresh=True)
        with patch.object(dfu, "setup", return_value=(installed, None)) as setup, \
                patch.object(dfu, "overview", lambda hook=True: overview(installed=True)):
            page.install_tile.click()
            self.settle(4)
        setup.assert_called_once()
        self.assertIn("Daggerfall Unity is installed", window.messages[-1])
        self.assertTrue(page.play.isVisibleTo(page))
        self.assertFalse(window.busy)

    def test_without_steams_daggerfall_the_blue_action_opens_steam(self):
        window, page = self.open_sheet(overview(steam=False))
        self.assertEqual(page.data_row.badge.text(), "Not installed")
        self.assertFalse(page.install_tile.isVisibleTo(page))
        self.assertFalse(page.play.isVisibleTo(page))
        self.assertTrue(page.steam_tile.isVisibleTo(page))
        self.assertIs(page.preferred_focus(), page.steam_tile)
        with patch("modsync.ui.pages.daggerfall.QDesktopServices.openUrl") as open_url:
            page.steam_tile.click()
        self.assertEqual(open_url.call_args.args[0].toString(), f"steam://install/{dfu.DAGGERFALL_APPID}")
        with patch.object(dfu, "overview", lambda hook=True: overview()):  # Steam installed it meanwhile
            page.refresh()
            self.settle(3)
        self.assertFalse(page.steam_tile.isVisibleTo(page))
        self.assertTrue(page.install_tile.isVisibleTo(page))

    def test_rows_switch_the_play_button_and_sync(self):
        window, page = self.open_sheet(overview(installed=True))
        self.assertEqual(page.hook_row.badge.text(), "Original game")
        with patch.object(dfu, "enable_hook", return_value="Launch hook enabled.") as enable:
            page.hook_row.click()
            self.settle(4)
        enable.assert_called_once()
        self.assertEqual(enable.call_args.args[0].path, Path("/games/Daggerfall Unity"))
        with patch.object(window.service, "share_game_folder", return_value=Path("/games")) as share:
            page.mods_row.click()
            self.settle(4)
        share.assert_called_once_with("dfu-mods")
        self.assertIn("mods now sync", window.messages[-1])

    def test_turning_things_off_again(self):
        window, page = self.open_sheet(overview(installed=True, hook_on=True, saves="on"))
        self.assertEqual(page.hook_row.badge.text(), "Daggerfall Unity")
        self.assertEqual(page.saves_row.badge.text(), "On")
        with patch.object(dfu, "disable_hook", return_value="Launch hook off.") as disable:
            page.hook_row.click()
            self.settle(4)
        disable.assert_called_once()
        with patch.object(window.service, "stop_game_folder", return_value=True) as stop:
            page.saves_row.click()
            self.settle(4)
        stop.assert_called_once_with("dfu-saves")
        self.assertIn("Nothing was deleted", window.messages[-1])

    def test_play_starts_it(self):
        window, page = self.open_sheet(overview(installed=True))
        self.assertIs(page.preferred_focus(), page.play)
        with patch.object(dfu, "launch", return_value="Starting Daggerfall Unity.") as launch:
            page.play.click()
            self.settle(4)
        launch.assert_called_once()
        self.assertEqual(window.messages[-1], "Starting Daggerfall Unity.")

    def test_a_version_mismatch_is_said_plainly(self):
        problem = "The synced mods are for Daggerfall Unity 1.2.0, but this machine has 1.1.1."
        window, page = self.open_sheet(overview(installed=True, mods="on", version_problem=problem))
        self.assertTrue(page.note.isVisibleTo(page))
        self.assertEqual(page.note.text(), problem)

    def test_a_failure_is_shown_and_the_window_is_free_again(self):
        window, page = self.open_sheet(overview(installed=True))
        with patch.object(window.service, "share_game_folder", side_effect=RuntimeError("Syncthing didn't start")):
            page.saves_row.click()
            self.settle(4)
        self.assertIn("Syncthing didn't start", window.messages[-1])
        self.assertFalse(window.busy)


class DaggerfallReviewTests(UiTestCase):
    """From GPT-6.1 Sol's review of #116."""

    def setUp(self):
        super().setUp()
        self.ov = overview(installed=True)
        p = patch.object(dfu, "overview", lambda hook=True: self.ov)
        p.start()
        self.addCleanup(p.stop)

    def test_a_queued_disable_can_be_undone_from_the_row(self):
        hook = launchhook.LaunchHookStatus(DAGGERFALL, True, "modsync_1812390_proton", True, None, None, True,
                                           launchhook.Pending("restore", DAGGERFALL.appid, None), True, True)
        self.ov = overview(installed=True)
        self.ov.hook = hook
        window = self.window()
        window.go("daggerfall")
        self.settle(3)
        page = window.pages["daggerfall"]
        with patch.object(dfu, "enable_hook", return_value="ok") as enable, \
                patch.object(dfu, "disable_hook") as disable:
            page.hook_row.click()
            self.settle(4)
        enable.assert_called_once()
        disable.assert_not_called()

    def test_the_version_comes_with_the_rest(self):
        self.ov = overview(installed=True)
        self.ov.version = "1.1.1"
        window = self.window()
        window.go("daggerfall")
        self.settle(3)
        self.assertEqual(window.pages["daggerfall"].install_row.badge.text(), "1.1.1")

    def test_the_mods_folder_is_opened_off_the_ui_thread(self):
        window = self.window()
        window.go("daggerfall")
        self.settle(3)
        with patch.object(dfu, "open_mods_folder", return_value=Path("/games/dfu/Mods")) as opener, \
                patch("modsync.ui.pages.daggerfall.QDesktopServices.openUrl") as open_url:
            window.pages["daggerfall"].mods_folder.click()
            self.settle(4)
        opener.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toLocalFile(), "/games/dfu/Mods")
        with patch.object(dfu, "open_mods_folder", side_effect=dfu.DfuError("Permission denied")):
            window.pages["daggerfall"].mods_folder.click()
            self.settle(4)
        self.assertIn("Permission denied", window.messages[-1])

    def test_the_picker_says_what_changed_on_daggerfalls_home(self):
        self.ov = overview()
        window = self.window()
        window.go("daggerfall")
        self.settle(3)
        self.ov = overview(installed=True, saves="on")
        window.pages["daggerfall"].handle_action(Action.ALT)  # Y: check again
        self.settle(3)
        window.open_game_picker()
        self.settle()
        row = window.top_overlay.rows["daggerfall"]
        self.assertEqual(row.badge.text(), "Ready")
        self.assertIn("saves", row.description)
        self.assertTrue(row.end.check)
