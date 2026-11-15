"""A game from the table as Home shows it (modsync.ui.pages.managed), with
modsync.mo2games faked: Starfield from Steam. Cyberpunk's own page is the same
class (tests/test_ui_cyberpunk.py covers Heroic's words)."""

from pathlib import Path
from unittest.mock import patch

from modsync import launchhook, mo2games
from modsync.mo2games.steam import SteamGame
from modsync.service import ModSyncService
from modsync.state import State
from modsync.ui.input import Action, InputRouter
from modsync.ui.overlays import ConfirmSheet, GameSheet
from tests.ui_support import UiTestCase

INSTANCE = Path("/games/ModOrganizer2-starfield")
GAME = SteamGame(1716740, Path("/steam/steamapps/common/Starfield"), Path("/steam/steamapps"), "23518663")
DECK = ["Steam Deck"]


def hook(*, on=False, pending=None, mapping=None):
    return launchhook.LaunchHookStatus(mo2games.get("starfield").game, on, mapping, on, None, None, False,
                                       pending, True, True)


def overview(*, here=True, instance=True, hook_status=None, mods="off", prefix=True, machines=(),
             kept_bytes=None, kept_versions=()):
    return mo2games.Overview(GAME if here else None, INSTANCE if instance else None, instance, None,
                             hook_status if hook_status is not None else hook(), mods,
                             ("1.16.244.0", "1.16.244.0") if here else None, None, prefix_ready=here and prefix,
                             machines=list(machines), kept_bytes=kept_bytes, kept_versions=list(kept_versions))


class ManagedGameUiTests(UiTestCase):
    def setUp(self):
        super().setUp()
        self.game = mo2games.get("starfield")

    def listed(self, ov, *, installed=True):
        """Starfield standing as ``ov`` says, and listed in the switcher."""
        for p in (patch.object(mo2games, "listed", lambda elsewhere=None: [self.game]),
                  patch.object(mo2games, "here", lambda: [self.game] if installed else []),
                  patch.object(self.game, "installs", lambda: [GAME] if installed else []),
                  patch.object(self.game, "overview", lambda hook=True, machines=None: ov)):
            p.start()
            self.addCleanup(p.stop)

    def open_page(self, ov, **kw):
        self.listed(ov, **kw)
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

    # --- which games the switcher lists ----------------------------------------------

    def test_with_nothing_set_up_the_other_games_are_under_add_a_game(self):
        window = self.window()
        self.settle(3)
        sheet = self.picker(window)
        self.assertEqual(list(sheet.rows), ["skyrim", "daggerfall"])
        self.assertEqual(sheet.extra.text(), "Add a game…")
        sheet.choose("add-game")
        self.settle(2)
        added = window.top_overlay
        self.assertEqual(added.title.text(), "Add a game")
        self.assertEqual(len(added.rows), len(mo2games.all_games()))
        self.assertIsNotNone(added.area)  # 22 games: the sheet scrolls

    def test_an_installed_game_without_mods_comes_first_under_add_a_game(self):
        for p in (patch.object(mo2games, "here", lambda: [self.game]),
                  patch.object(self.game, "installs", lambda: [GAME])):
            p.start()
            self.addCleanup(p.stop)
        window = self.window()
        self.settle(3)
        self.assertNotIn("mo2-starfield", self.picker(window).rows)
        window.top_overlay.choose("add-game")
        self.settle(2)
        added = window.top_overlay
        first = next(iter(added.rows))
        self.assertEqual(first, "mo2-starfield")
        self.assertEqual(added.rows[first].badge.text(), "Installed")
        self.assertIn("In Steam.", added.rows[first].description)
        added.choose("mo2-starfield")
        self.settle(3)
        self.assertIs(window.page, window.pages["mo2-starfield"])
        self.assertIn("mo2-starfield", self.picker(window).rows)  # stays while the window is open

    def test_a_game_set_up_here_is_listed_with_how_it_stands(self):
        self.listed(overview(instance=False))
        window = self.window()
        self.settle(3)
        row = self.picker(window).rows["mo2-starfield"]
        self.assertEqual(row.badge.text(), "No mods yet")
        self.assertIn("Install Mod Organizer 2", row.description)

    def test_a_game_kept_here_is_listed_even_though_it_isnt_installed(self):
        self.listed(overview(here=False, instance=False, mods="kept", machines=DECK, kept_bytes=3_200_000_000),
                    installed=False)
        window = self.window()
        self.settle(3)
        row = self.picker(window).rows["mo2-starfield"]
        self.assertEqual(row.badge.text(), "Kept")
        self.assertEqual(row.description, "For Steam Deck · 3.2 GB.")

    def test_a_game_another_machine_mods_is_listed_with_that_machine(self):
        self.listed(overview(here=False, instance=False, machines=["Steam Deck", "Laptop"]), installed=False)
        window = self.window()
        self.settle(3)
        row = self.picker(window).rows["mo2-starfield"]
        self.assertEqual(row.badge.text(), "Not here")
        self.assertEqual(row.description, "Modded on Steam Deck and Laptop.")

    def test_the_switcher_asks_the_service_which_games_the_other_machines_sync(self):
        seen = []

        def listed(elsewhere=None):
            seen.append(elsewhere)
            return []

        with patch.object(ModSyncService, "games_elsewhere", lambda self: {"starfield-mo2": DECK}), \
                patch.object(mo2games, "listed", listed):
            self.window()
            self.settle(3)
        self.assertIn({"starfield-mo2": DECK}, seen)

    # --- the page ------------------------------------------------------------------------

    def test_home_shows_the_game_with_steams_words(self):
        window, page = self.open_page(overview())
        self.assertIs(window.page, page)
        self.assertEqual(window.topbar.game_switch.text(), "Starfield")
        self.assertIn("From Steam.", page.head.subtitle.text())
        self.assertEqual(page.game_row.badge.text(), "1.16.244.0")
        self.assertEqual(page.hook_row.text(), "Steam's Play button")
        self.assertEqual(page.hook_row.badge.text(), "Game only")
        self.assertFalse(page.store_tile.isVisibleTo(page))
        self.assertIs(page.preferred_focus(), page.play)
        self.assertEqual(State.load().home_game, "mo2-starfield")

    def test_home_reopens_on_the_game_it_was_left_on(self):
        State.update(home_game="mo2-starfield")
        self.listed(overview())
        window = self.window()
        self.settle(3)
        self.assertIs(window.page, window.pages["mo2-starfield"])

    def test_reopening_on_a_kept_game_stays_on_it(self):
        State.update(home_game="mo2-starfield")
        self.listed(overview(here=False, instance=False, mods="kept", machines=DECK), installed=False)
        window = self.window()
        self.settle(3)
        self.assertIs(window.page, window.pages["mo2-starfield"])

    def test_a_game_that_left_this_machine_opens_on_skyrim(self):
        State.update(home_game="mo2-starfield")
        window = self.window()  # Steam doesn't have it any more, and nothing keeps it
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

    def test_a_kept_game_says_for_whom_and_offers_to_install_it(self):
        ov = overview(here=False, instance=False, mods="kept", machines=DECK, kept_bytes=3_200_000_000,
                      kept_versions=[("1.16.244.0", "1.16.244.0")])
        window, page = self.open_page(ov, installed=False)
        self.assertEqual(page.game_row.badge.text(), "Not installed")
        self.assertEqual(page.mo2_row.description, "Install Starfield first, then Mod Organizer 2 for it.")
        self.assertEqual(page.mods_row.badge.text(), "Kept")
        self.assertIn("Kept here for Steam Deck (3.2 GB)", page.mods_row.description)
        self.assertTrue(page.mods_row.isEnabled())
        self.assertTrue(page.info.isVisibleTo(page))
        self.assertEqual(page.info.text(), "The mods kept here are set up for Starfield 1.16.244.0.")
        self.assertTrue(page.store_tile.isVisibleTo(page))
        self.assertEqual(page.store_tile.text(), "Install Starfield in Steam")
        self.assertIn("already kept here", page.store_tile.description)
        self.assertIs(page.preferred_focus(), page.store_tile)
        with patch("modsync.ui.pages.managed.QDesktopServices.openUrl") as opened:
            page.store_tile.click()
        self.assertEqual(opened.call_args.args[0].toString(), "steam://install/1716740")

    def test_stopping_a_kept_copy_asks_first(self):
        window, page = self.open_page(overview(here=False, instance=False, mods="kept", machines=DECK),
                                      installed=False)
        with patch.object(window.service, "stop_game_folder", return_value=True) as stop:
            page.mods_row.click()
            self.settle(2)
            sheet = window.top_overlay
            self.assertIsInstance(sheet, ConfirmSheet)
            self.assertEqual(sheet.title.text(), "Stop keeping Starfield's mods?")
            sheet.cancel()
            self.settle(2)
            stop.assert_not_called()
            page.mods_row.click()
            self.settle(2)
            window.top_overlay.choose("stop")
            self.settle(4)
        stop.assert_called_once_with("starfield-mo2")
        self.assertIn("no longer kept here", window.messages[-1])

    def test_a_game_another_machine_mods_can_be_kept_here_for_it(self):
        window, page = self.open_page(overview(here=False, instance=False, machines=DECK), installed=False)
        self.assertEqual(page.mods_row.badge.text(), "Off")
        self.assertIn("Modded on Steam Deck. Press to keep a copy here", page.mods_row.description)
        self.assertIn("come over once Mod Organizer 2 is installed", page.store_tile.description)
        with patch.object(window.service, "hold_game_folder", return_value=Path("/held")) as hold:
            page.mods_row.click()
            self.settle(4)
        hold.assert_called_once_with("starfield-mo2")
        self.assertEqual(window.messages[-1], "A copy of your Starfield mods is kept here for Steam Deck.")

    def test_a_kept_copy_with_mo2_installed_offers_to_sync_here_instead(self):
        window, page = self.open_page(overview(mods="kept", machines=DECK))
        self.assertIn("Press to sync your mods with this Mod Organizer 2 instead", page.mods_row.description)
        with patch.object(window.service, "share_game_folder", return_value=INSTANCE) as share:
            page.mods_row.click()
            self.settle(4)
        share.assert_called_once_with("starfield-mo2")

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

    def test_words_for_machines_and_sizes(self):
        from modsync.ui.pages.managed import machines_text, size_text

        self.assertEqual(machines_text([]), "your other machines")
        self.assertEqual(machines_text(["Deck", "Laptop", "Desktop"]), "Deck, Laptop and Desktop")
        self.assertEqual(size_text(3_249_000_000), "3.2 GB")
        self.assertEqual(size_text(48_000_000), "48 MB")
        self.assertEqual(size_text(10), "under 1 MB")
        self.assertEqual(size_text(None), "")
