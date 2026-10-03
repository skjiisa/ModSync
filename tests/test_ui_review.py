"""Regressions found while reviewing the controller UI rewrite."""

from threading import Event
from unittest.mock import Mock, patch

from modsync import launchhook
from modsync.games import SKYRIM_SE
from modsync.pairing_lan import Announcement
from modsync.state import State
from tests.ui_support import UiTestCase


class HiddenScanResultsTests(UiTestCase):
    def test_instances_found_on_home_are_visible_when_mods_opens(self):
        path = str(self.tmp / "MO2")
        with patch("modsync.ui.pages.mods.scan_instances", return_value=[path]):
            window = self.window()
        chooser = window.pages["mods"].chooser
        self.assertEqual(window._current, "home")
        window.go("mods")
        self.settle()
        self.assertTrue(chooser.found_tiles[0].isVisible())
        chooser.found_tiles[0].setFocus()
        self.assertIs(window.focusWidget(), chooser.found_tiles[0])

    def test_peers_found_after_leaving_sync_appear_on_return(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        window.go("sync")
        sync = window.pages["sync"]
        with patch("modsync.ui.pages.sync.worker.run_async"):
            sync.toggle_join()
        window.go("home")
        sync.join.on_scanned([Announcement("Deck", "192.0.2.2", 21029, "session")])
        window.go("sync")
        self.settle()
        tile = sync.join.machine_tiles[0]
        self.assertTrue(tile.isVisible())
        tile.click()
        self.assertEqual(window.top_overlay.title.text(), "Pair with Deck")


class SetupChangeTests(UiTestCase):
    def test_reset_and_stop_sync_block_other_work_and_recover_after_failure(self):
        for operation in ("reset", "stop_sync"):
            for fail in (False, True):
                with self.subTest(operation=operation, fail=fail):
                    State(instance_path=str(self.tmp), folder_id="vault").save()
                    window = self.window(steam=launchhook.SteamLaunch(SKYRIM_SE))
                    release = Event()

                    def work():
                        if not release.wait(5):
                            raise RuntimeError("test operation timed out")
                        if fail:
                            raise RuntimeError("test failure")

                    with patch.object(window.service, operation, side_effect=work) as change:
                        if operation == "reset":
                            window.pages["system"]._reset()
                            window.top_overlay.choose("reset")
                        else:
                            window.pages["sync"].stop_sync()
                            window.top_overlay.choose("stop")
                        try:
                            self.assertTrue(window.busy)
                            self.assertFalse(window._timer.isActive())
                            self.assertFalse(window.pages["home"].play.isEnabled())
                            self.assertFalse(window.pages["game"].panel.isEnabled())
                            self.assertFalse(window.close())
                            self.assertIsNone(window.start_setup())
                            window.decide_launch(launchhook.EXIT_CONTINUE)
                            self.assertIsNone(window.steam_launch.decision)
                            window.change_setup(window.service.reset, message="Second change")
                        finally:
                            release.set()
                            self.settle(4)
                        change.assert_called_once()
                    self.assertFalse(window.busy)
                    self.assertTrue(window._timer.isActive())
                    self.assertTrue(window.pages["home"].play.isEnabled())
                    if fail:
                        self.assertIn("test failure", window.last_message)
                    window.close()

    def test_choosing_an_instance_locks_the_wizard_until_it_advances(self):
        window = self.window()
        setup = window.start_setup()
        release = Event()
        path = str(self.tmp / "MO2")
        with patch.object(window.service, "choose_instance", side_effect=lambda p: release.wait(5)) as choose:
            setup.chooser.use(path)
            try:
                self.assertTrue(window.busy)
                self.assertFalse(setup.chooser.isEnabled())
                self.assertFalse(window.close())
                setup.chooser.use(path)
            finally:
                release.set()
                self.settle(4)
        choose.assert_called_once_with(path)
        self.assertEqual(setup.index, 1)
        self.assertFalse(window.busy)
        self.assertTrue(setup.chooser.isEnabled())
        self.assertTrue(window._timer.isActive())

    def test_change_instance_uses_the_same_busy_guard(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        mods = window.pages["mods"]
        release = Event()
        with patch.object(window.service, "choose_instance", side_effect=lambda p: release.wait(5)) as choose:
            mods._change()
            window.top_overlay._use()
            try:
                self.assertTrue(window.busy)
                self.assertFalse(window.pages["home"].play.isEnabled())
            finally:
                release.set()
                self.settle(4)
        choose.assert_called_once_with(str(self.tmp))
        self.assertFalse(window.busy)
        self.assertIsNot(window.pages["mods"], mods)


class RuntimeInstallTests(UiTestCase):
    def test_runtime_install_blocks_launch_and_recovers_on_success_or_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                State(instance_path=str(self.tmp)).save()
                window = self.window()
                mods = window.pages["mods"]
                release = Event()

                def install():
                    if not release.wait(5):
                        raise RuntimeError("test operation timed out")
                    if fail:
                        raise RuntimeError("runtime test failure")
                    return "Runtime installed"

                with patch.object(window.service, "install_prefix_runtime", side_effect=install), \
                        patch.object(window.service, "launch_mo2") as launch:
                    mods._on_runtime_checked(["outdated"])
                    mods._install_runtime()
                    window.top_overlay.choose("go")
                    try:
                        self.assertTrue(window.busy)
                        self.assertFalse(window.pages["home"].play.isEnabled())
                        self.assertFalse(window.pages["game"].panel.isEnabled())
                        self.assertFalse(window.close())
                        window.launch(play=True)
                        launch.assert_not_called()
                        mods._on_runtime_checked(["outdated"])
                        self.assertFalse(mods.runtime_tile.isEnabled())
                    finally:
                        release.set()
                        self.settle(4)
                self.assertFalse(window.busy)
                self.assertTrue(window.pages["home"].play.isEnabled())
                window.close()


class BackgroundScrollTests(UiTestCase):
    def test_right_stick_stops_scrolling_after_the_app_loses_focus(self):
        from modsync.ui.input import InputRouter

        window = self.window()
        router = InputRouter.instance()
        area = Mock()
        with patch.object(router, "_app_active", return_value=True):
            router.pad_scroll(0.9)
        with patch.object(router, "_app_active", return_value=False), \
                patch("modsync.ui.nav.scroll_area_of", return_value=area):
            router._on_scroll_tick()
        area.verticalScrollBar.assert_not_called()
        self.assertFalse(router._scroll_timer.isActive())
        router.pad_scroll(0.0)
        self.assertTrue(window.isVisible())

    def test_a_bumper_held_for_a_short_tap_switches_only_one_section(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from modsync.ui.input import InputRouter

        window = self.window()
        router = InputRouter.instance()
        for pad_first in (False, True):
            with self.subTest(pad_first=pad_first):
                window.go("home")
                router._recent.clear()
                router._last_press = None
                with patch.object(router, "_app_active", return_value=True):
                    if pad_first:
                        router.pad_button("rb", True)
                    QTest.keyPress(window.focusWidget(), Qt.Key.Key_Alt, Qt.KeyboardModifier.AltModifier)
                    if not pad_first:
                        router.pad_button("rb", True)
                    QTest.qWait(220)
                    router.pad_button("rb", False)
                    QTest.keyRelease(window.focusWidget(), Qt.Key.Key_Alt)
                self.assertEqual(window._current, "game")


class PairingRestartTests(UiTestCase):
    def test_a_cancelled_attempt_cannot_end_a_new_pairing_attempt(self):
        State(instance_path=str(self.tmp), folder_id="vault").save()
        window = self.window()
        sync = window.pages["sync"]
        for outcome in ("on_done", "on_failed"):
            with self.subTest(outcome=outcome), patch("modsync.ui.pages.sync.worker.run_async") as run:
                sync.pair_network()
                old_job = run.call_args.kwargs
                sync.cancel_pairing()
                sync.pair_network()
                new_job = run.call_args.kwargs
                beacon = sync.beacon
                old_job[outcome]("old result")
                self.assertTrue(sync._pairing)
                self.assertIs(window.top_overlay, beacon)
                self.assertFalse(new_job["stop"].is_set())
                new_job["on_failed"]("new failure")
                self.assertFalse(sync._pairing)
                self.assertIsNone(window.top_overlay)
                self.assertIn("new failure", window.last_message)
