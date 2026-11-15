"""A game from the table as Home shows it (modsync.ui.pages.managed), with
modsync.mo2games faked: Starfield from Steam. Cyberpunk's own page is the same
class (tests/test_ui_cyberpunk.py covers Heroic's words)."""

from pathlib import Path
from unittest.mock import patch

from modsync import launchhook, mo2games
from modsync.mo2games.steam import SteamGame
from modsync.state import State
from modsync.ui.input import Action, InputRouter
from modsync.ui.overlays import GameSheet
from tests.ui_support import UiTestCase

INSTANCE = Path("/games/ModOrganizer2-starfield")
GAME = SteamGame(1716740, Path("/steam/steamapps/common/Starfield"), Path("/steam/steamapps"), "23518663")


def hook(*, on=False, pending=None, mapping=None):
    return launchhook.LaunchHookStatus(mo2games.get("starfield").game, on, mapping, on, None, None, False,
                                       pending, True, True)


def overview(*, here=True, instance=True, hook_status=None, mods="off", prefix=True):
    return mo2games.Overview(GAME if here else None, INSTANCE if instance else None, instance, None,
                             hook_status if hook_status is not None else hook(), mods,
                             ("1.16.244.0", "1.16.244.0") if here else None, None, prefix_ready=here and prefix)


class ManagedGameUiTests(UiTestCase):
    def setUp(self):
        super().setUp()
        self.game = mo2games.get("starfield")

    def here(self, ov):
        """Starfield on this machine, standing as ``ov`` says."""
        for p in (patch.object(mo2games, "here", lambda: [self.game]),
                  patch.object(self.game, "overview", lambda hook=True: ov)):
            p.start()
            self.addCleanup(p.stop)

    def open_page(self, ov):
        self.here(ov)
        window = self.window()
        self.settle(3)
        window.switch_game("mo2-starfield")
        self.settle(3)
        return window, window.pages["mo2-starfield"]

    def picker(self, window):
        InputRouter.instance().dispatch(Action.AUX)
        self.settle(2)
        self.assertIsInstance(window.top_overlay, GameSheet)
        return window.top_overlay

    def test_the_picker_offers_only_the_table_games_on_this_machine(self):
        window = self.window()  # nothing from the table here but Cyberpunk, always offered
        self.settle(3)
        self.assertEqual(list(self.picker(window).rows), ["skyrim", "daggerfall", "cyberpunk"])

    def test_a_game_steam_has_shows_up_in_the_picker_with_how_it_stands(self):
        self.here(overview(instance=False))
        window = self.window()
        self.settle(3)
        sheet = self.picker(window)
        self.assertEqual(list(sheet.rows), ["skyrim", "daggerfall", "cyberpunk", "mo2-starfield"])
        row = sheet.rows["mo2-starfield"]
        self.assertEqual(row.badge.text(), "No mods yet")
        self.assertIn("Install Mod Organizer 2", row.description)

    def test_home_shows_the_game_with_steams_words(self):
        window, page = self.open_page(overview())
        self.assertIs(window.page, page)
        self.assertEqual(window.topbar.game_switch.text(), "Starfield")
        self.assertIn("From Steam.", page.head.subtitle.text())
        self.assertEqual(page.game_row.badge.text(), "1.16.244.0")
        self.assertEqual(page.hook_row.text(), "Steam's Play button")
        self.assertEqual(page.hook_row.badge.text(), "Game only")
        self.assertIs(page.preferred_focus(), page.play)
        self.assertEqual(State.load().home_game, "mo2-starfield")

    def test_home_reopens_on_the_game_it_was_left_on(self):
        State.update(home_game="mo2-starfield")
        self.here(overview())
        window = self.window()
        self.settle(3)
        self.assertIs(window.page, window.pages["mo2-starfield"])

    def test_a_game_that_left_this_machine_opens_on_skyrim(self):
        State.update(home_game="mo2-starfield")
        window = self.window()  # Steam doesn't have it any more
        self.settle(3)
        self.assertIs(window.page, window.pages["home"])

    def test_before_mo2_the_blue_action_installs_it_for_this_game(self):
        window, page = self.open_page(overview(instance=False))
        self.assertTrue(page.install_tile.isVisibleTo(page))
        with patch.object(self.game, "install_mo2", return_value=INSTANCE) as install:
            page.install_tile.click()
            self.settle(4)
        self.assertEqual(install.call_args.args[0], self.game.default_instance_dir())
        self.assertIn("Mod Organizer 2 is installed", window.messages[-1])

    def test_steams_play_button_is_switched_and_waits_for_steam_to_close(self):
        window, page = self.open_page(overview())
        with patch.object(self.game, "enable_hook", return_value=["Launch hook installed; queued."]) as on:
            page.hook_row.click()
            self.settle(4)
        on.assert_called_once()
        pending = launchhook.Pending("select", 1716740, {"name": "modsync_1716740_proton"})
        page._show(overview(hook_status=hook(pending=pending)))
        self.assertEqual(page.hook_row.badge.text(), "Waiting for Steam")
        self.assertIn("once Steam has closed", page.hook_row.description)
        with patch.object(self.game, "disable_hook", return_value="Launch hook off.") as off:
            page.hook_row.click()  # changed their mind before Steam closed
            self.settle(4)
        off.assert_called_once()

    def test_mo2lints_own_tool_on_the_game_is_said_as_it_is(self):
        window, page = self.open_page(overview(hook_status=hook(mapping="mo2_1716740_proton_redirector")))
        self.assertEqual(page.hook_row.badge.text(), "Opens MO2")
        page._show(overview(hook_status=hook(on=True, mapping="modsync_1716740_proton")))
        self.assertEqual(page.hook_row.badge.text(), "Mod Organizer 2")
        self.assertIn("Steam cloud saves", page.play.description)

    def test_play_goes_through_the_games_own_launch(self):
        window, page = self.open_page(overview())
        with patch.object(self.game, "launch", return_value="Starting Starfield through Steam…") as launch:
            page.play.click()
            self.settle(4)
        self.assertTrue(launch.call_args.kwargs["play"])
        self.assertEqual(window.messages[-1], "Starting Starfield through Steam…")

    def test_mods_sync_uses_the_games_own_folder(self):
        window, page = self.open_page(overview())
        with patch.object(window.service, "share_game_folder", return_value=INSTANCE) as share:
            page.mods_row.click()
            self.settle(4)
        share.assert_called_once_with("starfield-mo2")
        self.assertIn("Starfield mods now sync", window.messages[-1])

    def test_a_synced_folders_row_leads_to_its_games_page(self):
        from modsync.ui.pages.sync import _folder_page

        self.assertEqual(_folder_page("starfield-mo2"), "mo2-starfield")
        self.assertEqual(_folder_page("cp2077-mo2"), "cyberpunk")
        self.assertEqual(_folder_page("dfu-saves"), "daggerfall")
        self.assertIsNone(_folder_page("something-else"))
        window = self.window()
        self.settle(3)
        window.show_game("mo2-starfield")  # not offered yet: its page is made on the way
        self.settle(3)
        self.assertIs(window.page, window.pages["mo2-starfield"])

    def test_a_long_name_is_cut_short_in_the_switcher_only(self):
        window = self.window()
        goty = mo2games.get("witcher3_goty")
        window._offer(goty)
        self.assertLessEqual(len(window.switch_name(goty.ui_key)), 26)
        self.assertTrue(window.switch_name(goty.ui_key).endswith("…"))
        self.assertIn((goty.ui_key, goty.name), [(key, name) for key, name, *_ in window.game_choices()])
