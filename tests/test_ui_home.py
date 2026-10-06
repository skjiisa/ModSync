"""Home setup entry points, maintenance lifecycle and Steam Deck layout."""

from dataclasses import replace
from threading import Event
from unittest.mock import patch

from modsync import gameversion, launchhook
from modsync.games import SKYRIM_SE
from modsync.service import ModSyncService, SyncStatus
from modsync.state import State
from tests.ui_support import UiTestCase, fake_game_status


class HomeSetupTests(UiTestCase):
    def test_install_and_existing_setup_are_available_without_the_wizard(self):
        window = self.window()
        home = window.pages["home"]
        self.assertEqual(list(window.tabs), ["home", "sync", "system"])
        self.assertEqual(window.tabs["system"].text(), "Settings")
        self.assertIs(window.focusWidget(), home.install_mo2)
        home.install_mo2.click()
        self.assertIsNone(window.setup)
        self.assertEqual(window.top_overlay.title.text(), "Install Mod Organizer 2")
        window.top_overlay.cancel()
        home.choose_mo2.click()
        self.assertIs(window.top_overlay.page, window.pages["mods"])
        self.assertEqual(window._current, "home")

    def test_missing_skse_installs_from_home_and_locks_launch_until_finished(self):
        State(instance_path=str(self.tmp)).save()
        version = gameversion.GameVersion.parse("1.6.1170")
        missing = replace(fake_game_status(), installed=version, expected=version, skse_runtime=None)
        check = gameversion.VersionCheck(version, version)
        with patch.object(ModSyncService, "game_status", return_value=missing), \
                patch.object(ModSyncService, "game_version_check", return_value=check):
            window = self.window()
            home = window.pages["home"]
            self.assertEqual(home.next_step.text(), "Install SKSE 2.2.6")
            release = Event()
            with patch.object(window.service, "install_skse", side_effect=lambda progress: release.wait(5)) as install:
                home.next_step.click()
                try:
                    self.assertTrue(window.busy)
                    self.assertFalse(home.play.isEnabled())
                    self.assertFalse(home.open_mo2.isEnabled())
                    self.assertTrue(home.work_bar.isVisible())
                    self.assertEqual(window._current, "home")
                    self.assertIsNone(window.top_overlay)
                finally:
                    release.set()
                    self.settle(4)
                install.assert_called_once()
            self.assertFalse(window.busy)
            self.assertTrue(home.play.isEnabled())

    def test_maintenance_preserves_the_page_and_returns_focus_to_its_home_row(self):
        window = self.window()
        home = window.pages["home"]
        home.game_row.setFocus()
        home.game_row.click()
        sheet = window.top_overlay
        page = window.pages["game"]
        self.assertIs(sheet.page, page)
        self.assertIs(window.scope(), sheet)
        page.panel.details_tile.click()
        self.assertIsNot(window.top_overlay, sheet)
        window.top_overlay.cancel()
        self.assertIs(window.top_overlay, sheet)
        sheet.cancel()
        self.settle()
        self.assertIs(window.pages["game"], page)
        self.assertGreaterEqual(window.stack.indexOf(page), 0)
        self.assertIs(window.focusWidget(), home.game_row)
        # Closing a sheet must disconnect its busy listener.
        window.set_busy("game", True)
        self.settle()
        window.set_busy("game", False)

    def test_maintenance_cannot_close_during_a_file_operation(self):
        window = self.window()
        window.go("game")
        sheet = window.top_overlay
        panel = window.pages["game"].panel
        panel._begin_file_operation("Restoring…")
        self.assertFalse(sheet.close_tile.isEnabled())
        sheet.cancel()
        self.assertIs(window.top_overlay, sheet)
        panel._end_file_operation()
        self.settle()
        sheet.cancel()
        self.assertIsNone(window.top_overlay)

    def test_selecting_an_instance_from_a_sheet_rebuilds_without_losing_it(self):
        with patch("modsync.ui.pages.mods.scan_instances", return_value=[str(self.tmp)]):
            window = self.window()
        window.pages["home"].choose_mo2.click()
        window.pages["mods"].chooser.found_tiles[0].click()
        self.settle(4)
        self.assertIsNone(window.top_overlay)
        self.assertEqual(window.service.state.instance_path, str(self.tmp))
        self.assertIsNotNone(window.pages["home"].open_mo2)
        self.assertEqual(list(window.tabs), ["home", "sync", "system"])

    def test_steam_controls_return_to_settings_in_the_same_order(self):
        window = self.window()
        settings = window.pages["system"]
        parent = settings.hook_tile.parentWidget()
        hook_layout, hook_index = settings.content_layout.itemAt(1).layout().itemAt(0).layout(), 1
        before = [hook_layout.itemAt(i).widget() for i in range(hook_layout.count())]
        window.pages["home"].steam_options.click()
        self.assertTrue(window.top_overlay.isAncestorOf(settings.hook_tile))
        window.top_overlay.cancel()
        self.assertIs(settings.hook_tile.parentWidget(), parent)
        after = [hook_layout.itemAt(i).widget() for i in range(hook_layout.count())]
        self.assertEqual(after, before)
        self.assertEqual(hook_layout.indexOf(settings.hook_tile), hook_index)
        window.pages["home"].steam_options.click()
        window.rebuild()
        self.settle()
        self.assertIsNone(window.top_overlay)
        window.set_busy("game", True)
        window.set_busy("game", False)

    def test_early_copy_route_skips_game_preparation(self):
        window = self.window()
        window.pages["home"].copy_setup.click()
        setup = window.setup
        self.assertTrue(setup.copy_from_machine)
        self.assertIn("Then pair", setup.head.subtitle.text())
        setup.chooser.use(str(self.tmp))
        self.settle(4)
        self.assertEqual(setup.index, 2)
        self.assertTrue(setup.join_panel.isVisibleTo(setup))
        self.assertFalse(setup.game.isVisibleTo(setup))
        from modsync.ui.input import Action
        setup.handle_action(Action.BACK)
        self.assertEqual(setup.index, 0)
        self.assertFalse(setup.game.isVisibleTo(setup))

    def test_runtime_repairs_are_available_directly_on_home(self):
        State(instance_path=str(self.tmp)).save()
        with patch.object(ModSyncService, "prefix_runtime_problems", return_value=["outdated"]):
            window = self.window()
        home = window.pages["home"]
        self.assertEqual(home.mo2_row.badge.text(), "Needs a fix")
        self.assertEqual(home.next_step.text(), "Install Visual C++ runtime…")
        home.next_step.click()
        self.assertEqual(window._current, "home")
        self.assertEqual(window.top_overlay.title.text(), "Install Visual C++ runtime")

    def test_missing_skse_is_recommended_but_can_be_skipped_in_guided_setup(self):
        missing = replace(fake_game_status(), skse_runtime=None)
        with patch.object(ModSyncService, "game_status", return_value=missing):
            window = self.window()
            setup = window.start_setup()
            setup.go_to(1)
            self.settle()
        self.assertIs(window.focusWidget(), setup.game.skse)
        self.assertEqual(setup.finish_tile.text(), "Continue without SKSE")
        setup.finish_tile.click()
        self.assertEqual(setup.index, 2)
        setup.local.click()
        self.settle()
        self.assertIsNone(window.setup)


class DeckHomeLayoutTests(UiTestCase):
    def test_core_actions_and_text_fit_without_scrolling_on_the_deck(self):
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QLabel
        from modsync.ui.widgets import Tile

        ready = fake_game_status()
        mismatch = replace(ready, expected=gameversion.GameVersion.parse("1.6.1170"))
        scenarios = [
            ("welcome", State(), ready, None),
            ("ready", State(instance_path=str(self.tmp)), ready, None),
            ("missing SKSE", State(instance_path=str(self.tmp)), replace(ready, skse_runtime=None), None),
            ("repair", State(instance_path=str(self.tmp)), mismatch, None),
            ("Steam and sync", State(instance_path=str(self.tmp), folder_id="vault"), mismatch,
             launchhook.SteamLaunch(SKYRIM_SE)),
        ]
        for name, state, status, steam in scenarios:
            with self.subTest(name=name), \
                    patch.object(State, "load", return_value=state), \
                    patch.object(ModSyncService, "game_status", return_value=status), \
                    patch.object(ModSyncService, "game_version_check",
                                 return_value=gameversion.VersionCheck(status.installed, status.expected)), \
                    patch.object(ModSyncService, "status", return_value=SyncStatus("ME", "vault", True, "syncing", 64.0, [])):
                window = self.window(steam=steam)
                self.settle(4)
                QTest.qWait(100)  # queued text changes and focus scrolling finish
                home = window.pages["home"]
                self.assertEqual((window.width(), window.height()), (1280, 800))
                self.assertEqual(home.area.verticalScrollBar().maximum(), 0)
                self.assertEqual(home.area.horizontalScrollBar().maximum(), 0)
                self.assertEqual(home.sync_row.isVisibleTo(home), state.syncing)
                viewport = home.area.viewport()
                for tile in home.findChildren(Tile):
                    if not tile.isVisibleTo(home):
                        continue
                    for point in (tile.rect().topLeft(), tile.rect().bottomRight()):
                        self.assertTrue(viewport.rect().contains(tile.mapTo(viewport, point)), tile.text())
                    for text in tile.findChildren(QLabel):
                        if text.isVisible() and text.wordWrap():
                            needed = text.heightForWidth(text.width())
                            self.assertGreaterEqual(text.height(), needed, text.text())
                    self.assertGreaterEqual(tile.desc.font().pointSizeF(), 12)
                window.close()
