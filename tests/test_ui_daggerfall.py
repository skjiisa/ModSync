"""The Daggerfall Unity sheet and its line on Home, with modsync.dfu faked."""

from pathlib import Path
from unittest.mock import patch

from modsync import dfu, launchhook
from modsync.games import DAGGERFALL
from modsync.ui.overlays import PageSheet
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
        """The sheet, with ``ov`` as what's set up for the rest of the test."""
        p = patch.object(dfu, "overview", lambda hook=True: ov)
        p.start()
        self.addCleanup(p.stop)
        window = self.window()
        window.go("daggerfall")
        self.settle(3)
        return window, window.pages["daggerfall"]

    def test_home_shows_the_line_only_where_daggerfall_is_there(self):
        for ov, shown in ((overview(steam=False), False), (overview(), True)):
            with self.subTest(shown=shown), patch.object(dfu, "overview", lambda hook=True, ov=ov: ov):
                window = self.window()
                self.settle(3)
                home = window.pages["home"]
                self.assertEqual(home.dfu_row.isVisibleTo(home), shown)
                window.close()

    def test_the_home_line_opens_the_sheet(self):
        with patch.object(dfu, "overview", lambda hook=True: overview(installed=True, mods="on")):
            window = self.window()
            self.settle(3)
            home = window.pages["home"]
            self.assertIn("Syncing mods", home.dfu_row.description)
            home.dfu_row.click()
            self.settle(3)
        self.assertTrue(any(isinstance(o, PageSheet) and o.page is window.pages["daggerfall"] for o in window.overlays))

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

    def test_without_steams_daggerfall_nothing_can_be_installed(self):
        window, page = self.open_sheet(overview(steam=False))
        self.assertEqual(page.data_row.badge.text(), "Not installed")
        self.assertFalse(page.install_tile.isEnabled())

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
