"""Home setup entry points, maintenance lifecycle and Steam Deck layout."""

from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from modsync import gameversion, launchhook
from modsync.games import SKYRIM_SE
from modsync.service import ModSyncService, SyncStatus
from modsync.state import State
from modsync.ui.input import Action
from tests.ui_support import UiTestCase, fake_game_status, handoff_plan

_GAME_STATUS = ModSyncService.game_status
_VERSION_CHECK = ModSyncService.game_version_check


class HomeSetupTests(UiTestCase):
    def test_missing_skse_keeps_play_primary_and_a_launches_instead_of_installing(self):
        for runtime in ("1.6.1170", "1.7.104"):
            for through_steam in (False, True):
                for delayed in (False, True):
                    with self.subTest(runtime=runtime, steam=through_steam, delayed=delayed):
                        State(instance_path=str(self.tmp)).save()
                        version = gameversion.GameVersion.parse(runtime)
                        missing = replace(fake_game_status(), installed=version, expected=version)
                        initial = replace(missing, skse_runtime=version) if delayed else missing
                        check = gameversion.VersionCheck(version, version)
                        steam = launchhook.SteamLaunch(SKYRIM_SE) if through_steam else None
                        with patch.object(ModSyncService, "game_status", return_value=initial), \
                                patch.object(ModSyncService, "game_version_check", return_value=check):
                            window = self.window(steam=steam)
                        window.router._set_mode("gamepad")
                        home = window.pages["home"]
                        if delayed:
                            window.pages["game"].panel._on_game_status(missing)
                            self.settle()
                        self.assertIs(window.focusWidget(), home.play)
                        self.assertEqual(home.play.property("tileRole"), "primary")
                        self.assertEqual(home.next_step.property("tileRole"), "normal")
                        self.assertTrue(home.next_step.isVisibleTo(home))
                        # Repeated polls and controller focus recovery keep Play the default.
                        home.on_game_checked(missing, check)
                        window.focus_scope_default()
                        self.assertIs(window.focusWidget(), home.play)
                        with patch.object(window.service, "install_skse") as install, \
                                patch.object(window.service, "launch_mo2", return_value="Started") as launch, \
                                patch.object(window.service, "prepare_mo2", return_value=(handoff_plan(self.tmp), "")) as prepare:
                            window.handle_action(Action.ACCEPT)
                            self.settle(4)
                        install.assert_not_called()
                        if through_steam:
                            prepare.assert_called_once_with(play=True)
                            self.assertEqual(steam.decision, launchhook.EXIT_CONTINUE)
                        else:
                            launch.assert_called_once_with(play=True)
                        window.close()

    def test_resolving_a_repair_does_not_leave_focus_on_optional_skse(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        st = replace(fake_game_status(), expected=gameversion.GameVersion.parse("1.6.1170"))
        home.on_game_checked(st, gameversion.VersionCheck(st.installed, st.expected))
        home.next_step.setFocus()
        st = replace(st, installed=st.expected)
        home.on_game_checked(st, gameversion.VersionCheck(st.installed, st.expected))
        self.assertIs(window.focusWidget(), home.play)
        self.assertEqual(home.play.property("tileRole"), "primary")

    def test_home_and_settings_share_steam_status_and_callbacks_preserve_busy_locks(self):
        window = self.window()
        settings, home = window.pages["system"], window.pages["home"]
        off = settings.hook
        on = replace(off, installed=True, selected=True, underlying_exists=True)
        pending = replace(off, pending=launchhook.Pending("select", SKYRIM_SE.appid, None))
        for st, badge in ((off, "Off"), (on, "On"), (pending, "Pending"),
                          (replace(on, underlying_exists=False), "Needs attention")):
            settings.on_hook_status(st)
            self.assertEqual(home.steam_options.badge.text(), badge)
        settings.on_hook_status(off)
        home.steam_options.click()
        window.set_busy("game", True)
        settings.on_hook_status(on)
        self.assertFalse(settings.hook_tile.isEnabled())
        with patch.object(launchhook, "enable") as enable, patch.object(launchhook, "disable") as disable:
            settings._toggle_hook()
        enable.assert_not_called()
        disable.assert_not_called()
        for callback in (settings._after_hook, settings._on_hook_failed):
            settings._hook_toggling = True
            callback("Test callback")
            self.assertFalse(settings.hook_tile.isEnabled())
        window.set_busy("game", False)
        self.assertTrue(settings.hook_tile.isEnabled())
        settings._hook_toggling = True
        settings.on_hook_status(off)
        self.assertFalse(settings.hook_tile.isEnabled())
        settings._hook_toggling = False
        window.top_overlay.cancel()

    def test_copy_waits_for_source_metadata_and_then_uses_its_version_after_reopening(self):
        from tests.test_service_instance import FakeManager
        from modsync.pairing_code import PairingCode

        installed = gameversion.GameVersion.parse("1.7.104")
        source = gameversion.GameVersion.parse("1.6.1170")
        game_dir = self.tmp / "game"
        game_dir.mkdir()
        (game_dir / "skse64_1_6_640.dll").write_bytes(b"")  # stale local SKSE must not set the target
        service = ModSyncService(manager=FakeManager())
        service.state = State(instance_path=str(self.tmp))
        gameversion.VaultMeta(SKYRIM_SE.appid, str(installed)).save(self.tmp)
        service.join_vault(PairingCode("PEER", "vault", "Source"), self.tmp)
        self.assertTrue(State.load().awaiting_vault_version)
        # Use actual service checks and a real metadata file; Steam and the game are synthetic.
        index = SimpleNamespace(from_version=str(installed), targets=[str(source), "1.6.640"], origin="test")
        with patch.object(ModSyncService, "game_status", _GAME_STATUS), \
                patch.object(ModSyncService, "game_version_check", _VERSION_CHECK), \
                patch.object(ModSyncService, "_steam_app", return_value=(None, None, None)), \
                patch("modsync.service.recipe.load_index", return_value=index), \
                patch.object(gameversion, "find_game_dir", return_value=game_dir), \
                patch.object(gameversion, "installed_version", return_value=installed):
            for _ in range(2):  # waiting survives closing and reopening
                service = ModSyncService(manager=FakeManager())
                window = self.window(service=service)
                home, panel = window.pages["home"], window.pages["game"].panel
                self.assertEqual(home.game_row.badge.text(), "Waiting")
                self.assertEqual(home.skse_row.badge.text(), "After sync")
                self.assertTrue(home.next_step.isHidden())
                for tile in (panel.downgrade, panel.skse, panel.pin, panel.adopt):
                    self.assertTrue(tile.isHidden())
                self.assertTrue(State.load().awaiting_vault_version)
                window.close()
            window = self.window(service=ModSyncService(manager=FakeManager()))
            gameversion.VaultMeta(SKYRIM_SE.appid, str(source)).save(self.tmp)
            window.pages["game"].panel.poll()  # detects metadata arriving before the rest of the mods
            self.settle(4)
            home = window.pages["home"]
            self.assertEqual(home.game_row.badge.text(), f"Needs {source}")
            self.assertEqual(home.next_step.text(), f"Downgrade to {source}…")
            self.assertFalse(State.load().awaiting_vault_version)
            with patch.object(window.pages["game"].panel, "_run_downgrade") as downgrade:
                home.next_step.click()
                self.assertEqual(window.top_overlay.title.text(), f"Downgrade Skyrim Special Edition to {source}")
                window.top_overlay.tiles["go"].click()
                downgrade.assert_called_once_with(str(source))

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
        ready.skse_runtime = ready.installed
        mismatch = replace(ready, expected=gameversion.GameVersion.parse("1.6.1170"))
        scenarios = [
            ("welcome", State(), ready, None),
            ("welcome without SKSE", State(), replace(ready, skse_runtime=None), None),
            ("ready", State(instance_path=str(self.tmp)), ready, None),
            ("missing SKSE", State(instance_path=str(self.tmp)), replace(ready, skse_runtime=None), None),
            ("waiting for source", State(instance_path=str(self.tmp), folder_id="vault", awaiting_vault_version=True),
             replace(ready, expected=None, awaiting_vault_version=True), None),
            ("repair", State(instance_path=str(self.tmp)), mismatch, None),
            ("Steam and sync", State(instance_path=str(self.tmp), folder_id="vault"), mismatch,
             launchhook.SteamLaunch(SKYRIM_SE)),
            ("Steam without SKSE", State(instance_path=str(self.tmp)), replace(ready, skse_runtime=None),
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
                settings = window.pages["system"]
                settings.on_hook_status(replace(settings.hook, installed=True, selected=True, underlying_exists=False))
                QTest.qWait(50)
                self.assertEqual(home.area.verticalScrollBar().maximum(), 0)
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
