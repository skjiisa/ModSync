"""Build the dashboard offscreen in each setup state. Catches wiring mistakes
(missing attributes, signals to dead slots) that only surface when Qt runs."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - GUI extra not installed
    QApplication = None

from modsync import background, gameversion, launchhook
from modsync.games import SKYRIM_SE
from modsync.service import GameStatus, ModSyncService, SyncStatus
from modsync.state import State


def fake_game_status(self, *, refresh_index=True):
    return GameStatus(
        installed=gameversion.GameVersion.parse("1.7.104"),
        expected=None,
        game_dir=Path("/games/Skyrim"),
        language="english",
        steam_public_build=1,
        steam_is_current=True,
        steam_running=False,
        pending_pin=False,
        recipe_from="1.7.104",
        recipe_targets=["1.6.1170"],
        recipe_origin="bundled",
    )


def fake_sync_status(self):
    return SyncStatus("ME", self.state.folder_id, True, "idle", 100.0, [])


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class _SmokeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        patches = [
            patch.object(ModSyncService, "game_status", fake_game_status),
            patch.object(
                ModSyncService,
                "game_version_check",
                lambda self: gameversion.VersionCheck(gameversion.GameVersion.parse("1.7.104"), None),
            ),
            patch.object(ModSyncService, "status", fake_sync_status),
            patch.object(ModSyncService, "my_pairing_code", lambda self: None),
            patch.object(ModSyncService, "accept_pending", lambda self: []),
            patch.object(background, "status", lambda: {"installed": False, "active": "inactive", "enabled": "disabled"}),
            patch.object(launchhook, "status", lambda appid=SKYRIM_SE.appid: launchhook.LaunchHookStatus(
                SKYRIM_SE, False, None, False, None, None, False, None, False, True)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class DashboardSmokeTests(_SmokeBase):
    def _settle(self):
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()

    def _build(self):
        from modsync.ui.dashboard import Dashboard

        dash = Dashboard(ModSyncService(manager=object()))
        dash.resize(1120, 780)
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        dash.shutdown()
        return dash

    def test_nothing_set_up(self):
        dash = self._build()
        self.assertFalse(dash.sync.live)
        self.assertIn("Skyrim version: 1.7.104", dash.game._label.text())
        self.assertIn("Steam launch: off", dash._hook_status.text())
        self.assertIn("Open ModSync before Skyrim", dash._hook_button.text())

    def test_first_run_warnings_are_readable_without_an_instance(self):
        from modsync.ui.game_card import GameCard

        service = ModSyncService(manager=object())
        card = GameCard(service)
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        installed = gameversion.GameVersion.parse("1.7.104")
        skse = gameversion.GameVersion.parse("1.5.97")
        check = gameversion.VersionCheck(
            installed, None,
            skse=gameversion.SkseCheck([
                gameversion.SkseFile(Path("skse64_1_5_97.dll"), skse, "game folder")
            ]),
        )
        with patch.object(service, "game_version_check", return_value=check):
            card._on_game_status(fake_game_status(service))
        self.assertIn("1.5.97", card._label.text())
        self.assertEqual(card._label.property("role"), "warning")
        status = fake_game_status(service)
        status.steam_is_current = False
        card._on_game_status(status)
        self.assertIn("Steam has an update ready", card._label.text())
        self.assertEqual(card._label.property("role"), "warning")
        card._on_game_status(fake_game_status(service))
        self.assertNotIn("Steam has an update ready", card._label.text())
        self.assertEqual(card._label.property("role"), "secondary")

    def test_firewall_row_toggles_between_allow_and_remove(self):
        from modsync.firewall import Check, Firewall
        from modsync.ui.dashboard import Dashboard

        State(instance_path=str(self.tmp)).save()
        blocked = Check(Firewall("ufw"), False, "ufw:1")
        with patch("modsync.ui.dashboard.firewall.check", return_value=blocked):
            dash = Dashboard(ModSyncService(manager=object()))
            QThreadPool.globalInstance().waitForDone(5000)
            self.app.processEvents()
        try:
            self.assertTrue(dash._fw_widget.isVisibleTo(dash))
            self.assertEqual(dash._fw_button.text(), "Allow in firewall…")
            self.assertEqual(dash._fw_status.property("role"), "warning")
            self.assertIn("21029/tcp", dash._fw_status.text())
            self.assertTrue(dash.sync._firewall is not None and not dash.sync._fw_allowed)

            # Allow: the stamp is remembered and the rules are re-read, not assumed.
            allowed = Check(Firewall("ufw"), True, "ufw:2")
            with patch("modsync.ui.dashboard.firewall.allow", return_value="ufw:2"), \
                    patch("modsync.ui.dashboard.firewall.check", return_value=allowed):
                dash._toggle_firewall()
                self._settle()  # allow finishes...
                self._settle()  # ...then the re-check it triggers
            self.assertEqual(State.load().firewall_rules_stamp, "ufw:2")
            self.assertEqual(dash._fw_button.text(), "Remove firewall rules…")
            self.assertEqual(dash._fw_status.property("role"), "secondary")
            self.assertTrue(dash.sync._fw_allowed)

            # Remove: stamp cleared, and whatever the rules say now is shown.
            with patch("modsync.ui.dashboard.firewall.revoke", return_value="ufw:3") as revoke, \
                    patch("modsync.ui.dashboard.firewall.check", return_value=blocked):
                dash._toggle_firewall()
                self._settle()
                self._settle()
            revoke.assert_called_once()
            self.assertEqual(State.load().firewall_rules_stamp, "")
            self.assertEqual(dash._fw_button.text(), "Allow in firewall…")

            # No firewall at all: the row disappears.
            dash._on_firewall_checked(Check(None, True, ""))
            self.assertFalse(dash._fw_widget.isVisibleTo(dash))
        finally:
            dash.shutdown()

    def test_instance_only(self):
        State(instance_path=str(self.tmp), instance_label="MO2").save()
        dash = self._build()
        self.assertFalse(dash.sync.live)
        self.assertTrue(hasattr(dash.sync, "_join_panel"))

    def test_syncing(self):
        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        dash = self._build()
        self.assertTrue(dash.sync.live)
        self.assertIn("100% in sync", dash.sync._folder_state.text())


if __name__ == "__main__":
    unittest.main()


class WizardSmokeTests(_SmokeBase):
    def _wizard(self):
        from modsync.ui.wizard import WizardWidget

        class NoInstaller:
            def available(self):
                return False, "test"

            def install(self, *a, **k):
                raise AssertionError("not called")

        svc = ModSyncService(manager=object())
        wizard = WizardWidget(svc, installer=NoInstaller())
        wizard.resize(1120, 780)
        return svc, wizard

    def _settle(self):
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()

    def test_not_now_finishes_with_instance_remembered(self):
        with patch("modsync.ui.wizard.ChooseInstancePage._scan", staticmethod(lambda: [])):
            svc, wizard = self._wizard()
        done = []
        wizard.completed.connect(done.append)
        wizard._on_next()  # welcome -> choose
        self._settle()
        wizard._choose._select(str(self.tmp))
        wizard._on_next()  # choose -> sync (commits the instance)
        self._settle()
        self.assertEqual(wizard._index, 2)
        self.assertIs(wizard._pages[2], wizard._vault)
        self.assertEqual(State.load().instance_path, str(self.tmp))
        self.assertFalse(State.load().syncing)
        self.assertEqual(wizard._vault.mode, "local")
        self.assertEqual(wizard._next.text(), "Next")  # keeping it local: game version is next
        wizard._on_next()  # sync -> game
        self._settle()
        self.assertIs(wizard._pages[wizard._index], wizard._game_page)
        wizard._on_next()  # finish
        self.assertEqual(done[0]["mode"], "local")
        self.assertEqual(done[0]["instance_path"], str(self.tmp))

    def test_copying_from_another_machine_finishes_at_the_sync_step(self):
        """A joiner can't know which game version its mods need until they've
        synced, so the wizard must not route it through the game-version page."""
        from modsync.pairing_code import PairingCode

        with patch("modsync.ui.wizard.ChooseInstancePage._scan", staticmethod(lambda: [])):
            svc, wizard = self._wizard()
        done = []
        wizard.completed.connect(done.append)
        wizard._on_next()  # welcome -> choose
        self._settle()
        wizard._choose._select(str(self.tmp))
        wizard._on_next()  # choose -> sync
        self._settle()
        wizard._vault.join_radio.setChecked(True)
        wizard._vault.code_edit.setText(PairingCode("A" * 56, "modsync-abc", "Deck").encode())
        self.assertEqual(wizard._next.text(), "Finish")
        wizard._on_next()
        self.assertEqual(done[0]["mode"], "join")
        self.assertEqual(wizard._index, 2)  # never showed the game-version page

    def test_cannot_switch_instance_while_syncing(self):
        State(instance_path="/elsewhere", folder_id="modsync-1").save()
        with patch("modsync.ui.wizard.ChooseInstancePage._scan", staticmethod(lambda: [])):
            svc, wizard = self._wizard()
        wizard._on_next()
        self._settle()
        wizard._choose._select(str(self.tmp))
        wizard._on_next()
        self._settle()
        self.assertEqual(wizard._index, 1)  # stayed put
        self.assertIn("stop syncing", wizard._choose._chosen.text())
        self.assertTrue(wizard._next.isEnabled())


class SteamLaunchSmokeTests(_SmokeBase):
    """Steam's launch hook opens the regular window with the launch waiting on it."""

    def _settle(self):
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()

    def _window(self, through=None):
        from modsync.ui.dashboard import Dashboard
        from modsync.ui.main_window import MainWindow

        launch = launchhook.SteamLaunch(SKYRIM_SE, through)
        with patch.object(Dashboard, "_scan_instances", return_value=[]):
            window = MainWindow(steam_launch=launch)
            window.show()
            self._settle()
        self.addCleanup(window.close)
        return window

    def test_play_becomes_continue_and_names_the_next_step(self):
        window = self._window(through="mo2_489830_redirector")
        dash = window._stack.currentWidget()
        self.assertEqual(dash._play_button.text(), "Continue to Mod Organizer")
        self.assertTrue(dash._play_button.isEnabled())  # no instance needed to go on
        self.assertTrue(dash._cancel_launch_button.isEnabled())
        self.assertIn("Mod Organizer 2 (MO2-LINT)", dash._steam_note.text())
        other = self._window(through="GE-Proton10-34")
        self.assertEqual(other._stack.currentWidget()._play_button.text(), "Continue to Skyrim Special Edition")

    def test_enter_continues_and_space_on_cancel_cancels(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            with self.subTest(key=key):
                window = self._window(through="mo2_489830_redirector")
                window.activateWindow()
                dash = window._stack.currentWidget()
                self.assertIs(self.app.focusWidget(), dash._play_button)
                QTest.keyClick(self.app.focusWidget(), key)
                self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        window = self._window()
        dash = window._stack.currentWidget()
        dash._cancel_launch_button.setFocus()
        QTest.keyClick(dash._cancel_launch_button, Qt.Key.Key_Return)  # Enter belongs to Continue only
        self.assertIsNone(window.steam_launch.decision)
        QTest.keyClick(dash._cancel_launch_button, Qt.Key.Key_Space)
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_launched_from_the_desktop_nothing_changes(self):
        from modsync.ui.main_window import MainWindow

        from modsync.ui.dashboard import Dashboard

        with patch.object(Dashboard, "_scan_instances", return_value=[]):
            window = MainWindow()
            window.show()
            self._settle()
        self.addCleanup(window.close)
        dash = window._stack.currentWidget()
        self.assertEqual(dash._play_button.text(), "Play Skyrim")
        self.assertIsNone(dash._cancel_launch_button)
        window.decide_launch(launchhook.EXIT_CONTINUE)  # no Steam launch to decide
        self.assertTrue(window.isVisible())

    def test_own_launches_are_left_to_steam(self):
        State(instance_path=str(self.tmp)).save()
        with patch.object(ModSyncService, "launch_mo2") as launch:
            window = self._window()
            dash = window._stack.currentWidget()
            self.assertIsNone(dash._open_mo2_button)
            dash._play_button.click()
            launch.assert_not_called()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        self.assertFalse(window.isVisible())

    def test_setup_summary_and_decisions(self):
        inst = self.tmp / "MO2"
        (inst / "profiles" / "Default").mkdir(parents=True)
        (inst / "mods").mkdir()
        (inst / "ModOrganizer.ini").write_text("[General]\ngameName=Skyrim Special Edition\nselected_profile=@ByteArray(Default)\n")
        (inst / "profiles/Default/modlist.txt").write_text("+SkyUI\n-Unused\n+USSEP\n")
        State(instance_path=str(inst), instance_label="My setup").save()
        window = self._window()
        dash = window._stack.currentWidget()
        self.assertIn("Profile: Default  ·  2 mods enabled", dash._setup_label.text())
        dash._play_button.click()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        window.decide_launch(launchhook.EXIT_CANCEL)  # a second decision does not overwrite the first
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CONTINUE)
        other = self._window()
        other._stack.currentWidget()._cancel_launch_button.click()
        self.assertEqual(other.steam_launch.decision, launchhook.EXIT_CANCEL)
        closed = self._window()
        closed.close()
        self.assertEqual(closed.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_decision_survives_a_trip_through_the_wizard(self):
        from modsync.ui.dashboard import Dashboard
        from modsync.ui.wizard import WizardWidget

        window = self._window(through="mo2_489830_redirector")
        with patch("modsync.ui.wizard.ChooseInstancePage._scan", return_value=[]):
            window._show_wizard()
        self.assertIsInstance(window._stack.currentWidget(), WizardWidget)
        with patch.object(Dashboard, "_scan_instances", return_value=[]):
            window._stack.currentWidget()._cancel.click()
            self._settle()
        dash = window._stack.currentWidget()
        self.assertIsInstance(dash, Dashboard)
        self.assertEqual(dash._play_button.text(), "Continue to Mod Organizer")
        self.assertIsNone(window.steam_launch.decision)
        window._show_wizard()
        window.close()  # closing from the wizard cancels too
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def test_unfinished_sync_is_flagged_next_to_continue(self):
        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        window = self._window()
        dash = window._stack.currentWidget()
        self.assertFalse(dash._sync_warning.isVisibleTo(dash))  # fake status: idle, 100%
        dash.sync._on_status(SyncStatus("ME", "modsync-1", True, "syncing", 64.0, []))
        self.assertTrue(dash._sync_warning.isVisibleTo(dash))
        self.assertIn("64%", dash._sync_warning.text())
        dash.sync._on_status(SyncStatus("ME", "modsync-1", True, "idle", 100.0, []))
        self.assertFalse(dash._sync_warning.isVisibleTo(dash))

    def test_usvfs_controls_apply_and_restore_while_steam_waits(self):
        from modsync.mo2 import usvfs

        State(instance_path=str(self.tmp)).save()
        current = usvfs.Status("available", "ARM64 fix available", True)
        messages = []

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
            dash = window._stack.currentWidget()
            dash._usvfs.status.connect(messages.append)
            self._settle()
            self.assertTrue(dash._usvfs.apply_button.isVisibleTo(dash))
            dash._usvfs.apply_button.click()
            for _ in range(3):
                self._settle()
            self.assertTrue(dash._usvfs.restore_button.isVisibleTo(dash))
            dash._usvfs.restore_button.click()
            for _ in range(3):
                self._settle()
            self.assertEqual(messages, ["applied", "restored"])
            self.assertTrue(dash._usvfs.apply_button.isVisibleTo(dash))
            self.assertTrue(dash._play_button.isEnabled())
            self.assertIsNone(window.steam_launch.decision)

    def test_auto_decision_env_is_a_testing_aid(self):
        from PySide6.QtTest import QTest

        with patch.dict(os.environ, {"MODSYNC_HUB_AUTO_DECISION": "cancel"}):
            window = self._window()
        self.assertIn("Test mode", window._stack.currentWidget()._status_line.text())
        QTest.qWait(3500)
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)


class FileOperationNavigationTests(_SmokeBase):
    def test_usvfs_replacement_blocks_launch_and_navigation(self):
        from threading import Event
        from modsync.mo2 import usvfs
        from modsync.ui.dashboard import Dashboard
        from modsync.ui.main_window import MainWindow

        State(instance_path=str(self.tmp)).save()
        for steam in (False, True):
            with self.subTest(steam_launch=steam):
                release = Event()
                available = usvfs.Status("available", "ARM64 fix available", True)
                with patch.object(usvfs, "is_arm64", return_value=True), \
                        patch.object(ModSyncService, "usvfs_status", return_value=available), \
                        patch.object(ModSyncService, "apply_usvfs_fix", side_effect=lambda: release.wait(5)), \
                        patch.object(ModSyncService, "launch_mo2") as launch, \
                        patch.object(Dashboard, "_scan_instances", return_value=[]):
                    window = MainWindow(steam_launch=launchhook.SteamLaunch(SKYRIM_SE) if steam else None)
                    window.show()
                    self._settle()
                    dash = window._stack.currentWidget()
                    try:
                        dash._usvfs.apply_button.click()
                        self.assertTrue(dash._usvfs.busy)
                        self.assertTrue(dash.busy)
                        self.assertFalse(dash._play_button.isEnabled())
                        self.assertFalse(dash._wizard_button.isEnabled())
                        self.assertFalse(dash.game.isEnabled())
                        self.assertFalse(window.close())
                        if steam:
                            self.assertFalse(dash._cancel_launch_button.isEnabled())
                            window.decide_launch(launchhook.EXIT_CONTINUE)
                            window.decide_launch(launchhook.EXIT_CANCEL)
                            self.assertIsNone(window.steam_launch.decision)
                        else:
                            dash._launch_mo2(play=True)
                            launch.assert_not_called()
                    finally:
                        release.set()
                        self._settle()
                        self._settle()
                        window.close()
                if steam:
                    self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)

    def _settle(self):
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()

    def _window(self):
        from modsync.ui.main_window import MainWindow

        with patch("modsync.ui.dashboard.Dashboard._scan_instances", return_value=[]):
            window = MainWindow()
        window.show()
        self._settle()
        self.addCleanup(window.close)
        return window

    def test_dashboard_blocks_navigation_and_close_during_file_changes(self):
        from threading import Event
        from PySide6.QtWidgets import QMessageBox

        for operation in ("downgrade", "restore"):
            for fail in (False, True):
                with self.subTest(operation=operation, fail=fail):
                    window = self._window()
                    dash = window._stack.currentWidget()
                    card = dash.game
                    card.game.expected = gameversion.GameVersion.parse("1.6.1170")
                    release = Event()

                    def work(*args):
                        if not release.wait(5):
                            raise RuntimeError("test operation timed out")
                        if fail:
                            raise RuntimeError("test failure")
                        return object()

                    method = "run_downgrade" if operation == "downgrade" else "restore_game_files"
                    with patch.object(window.service, method, side_effect=work), patch.object(
                        QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
                    ):
                        try:
                            if operation == "downgrade":
                                card._start_downgrade()
                            else:
                                card._restore_files()
                            self.assertTrue(card.busy)
                            self.assertFalse(dash._wizard_button.isEnabled())
                            self.assertFalse(dash.mo2.isEnabled())
                            self.assertFalse(dash.sync.isEnabled())
                            window._show_wizard()
                            window._show_dashboard()
                            self.assertIs(window._stack.currentWidget(), dash)
                            self.assertFalse(window.close())
                            # A status response already in flight must not re-enable actions.
                            card._on_game_status(card.game)
                            self.assertFalse(card._refresh_btn.isEnabled())
                            self.assertTrue(card._restore.isHidden())
                            self.assertTrue(card._downgrade.isHidden())
                        finally:
                            release.set()
                            self._settle()
                            self._settle()
                    self.assertFalse(card.busy)
                    self.assertTrue(dash._wizard_button.isEnabled())
                    self.assertTrue(dash.mo2.isEnabled())
                    self.assertTrue(dash.sync.isEnabled())
                    self.assertTrue(card._refresh_btn.isEnabled())
                    if fail:
                        self.assertIn("test failure", dash._status_line.text())
                    self.assertTrue(window.close())

    def test_wizard_install_blocks_window_close_and_recovers_on_failure(self):
        from threading import Event

        window = self._window()
        dashboard = window._stack.currentWidget()
        with patch("modsync.ui.wizard.ChooseInstancePage._scan", return_value=[]):
            window._show_wizard()
            wizard = window._stack.currentWidget()
            wizard._next.click()
        self.assertFalse(dashboard._timer.isActive())
        self._settle()
        release = Event()

        def install(*args, **kwargs):
            if not release.wait(5):
                raise RuntimeError("test operation timed out")
            raise RuntimeError("installer failed")

        with patch.object(wizard._choose._installer, "available", return_value=(True, "")), patch.object(
            wizard._choose._installer, "install", side_effect=install
        ):
            try:
                wizard._choose._start_install()
                self.assertTrue(wizard.busy)
                wizard._on_back()
                wizard._on_next()
                self.assertEqual(wizard._index, 1)
                self.assertFalse(window.close())
                self.assertFalse(wizard._back.isEnabled())
            finally:
                release.set()
                self._settle()
        self.assertFalse(wizard.busy)
        self.assertTrue(wizard._back.isEnabled())
        self.assertIn("installer failed", wizard._choose._log.toPlainText())
        self.assertTrue(window.close())

    def test_steam_launch_cannot_continue_or_close_during_restore(self):
        from threading import Event
        from PySide6.QtWidgets import QMessageBox
        from modsync.ui.dashboard import Dashboard
        from modsync.ui.main_window import MainWindow

        with patch.object(Dashboard, "_scan_instances", return_value=[]):
            window = MainWindow(steam_launch=launchhook.SteamLaunch(SKYRIM_SE))
            window.show()
            self._settle()
        dash = window._stack.currentWidget()
        release = Event()
        with patch.object(window.service, "restore_game_files", side_effect=lambda: release.wait(5)), patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            try:
                dash.game._restore_files()
                self.assertFalse(dash._play_button.isEnabled())
                self.assertFalse(dash._cancel_launch_button.isEnabled())
                window.decide_launch(launchhook.EXIT_CONTINUE)
                window.decide_launch(launchhook.EXIT_CANCEL)
                self.assertFalse(window.close())
                self.assertIsNone(window.steam_launch.decision)
            finally:
                release.set()
                self._settle()
                self._settle()
        self.assertTrue(dash._play_button.isEnabled())
        dash._cancel_launch_button.click()
        self.assertEqual(window.steam_launch.decision, launchhook.EXIT_CANCEL)


class UiNavigationTests(_SmokeBase):
    def test_install_button_opens_install_page_and_can_return_to_dashboard(self):
        from PySide6.QtWidgets import QPushButton
        from modsync.ui.main_window import MainWindow
        from modsync.ui.dashboard import Dashboard
        from modsync.ui.wizard import WizardWidget

        with patch.object(Dashboard, "_scan_instances", return_value=[]), patch(
            "modsync.ui.wizard.ChooseInstancePage._scan", return_value=[]
        ):
            window = MainWindow()
            window.show()
            self.addCleanup(window.close)
            QThreadPool.globalInstance().waitForDone(5000)
            self.app.processEvents()
            dashboard = window._stack.currentWidget()
            next(b for b in dashboard.findChildren(QPushButton) if b.text() == "Install MO2…").click()
            wizard = window._stack.currentWidget()
            self.assertIsInstance(wizard, WizardWidget)
            self.assertEqual(wizard._index, 1)
            self.assertFalse(wizard._choose._panel.isHidden())
            self.assertEqual(wizard._steps.text(), "STEP 1 OF 3")
            wizard._set_busy(True)
            self.assertFalse(wizard._cancel.isEnabled())
            wizard._on_cancel()
            self.assertIs(window._stack.currentWidget(), wizard)
            wizard._set_busy(False)
            wizard._cancel.click()
            self.assertIsInstance(window._stack.currentWidget(), Dashboard)
            QThreadPool.globalInstance().waitForDone(5000)
            self.app.processEvents()


class GameWarningCopyTests(_SmokeBase):
    def _card(self):
        from modsync.ui.game_card import GameCard

        service = ModSyncService(manager=object())
        card = GameCard(service)
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        return service, card

    def test_skse_warning_is_short_and_keeps_technical_details_in_tooltip(self):
        service, card = self._card()
        version = gameversion.GameVersion.parse
        status = fake_game_status(service)
        status.installed = version("1.6.1170")
        status.skse_runtime = version("1.5.97")
        status.recipe_targets = ["1.5.97"]
        status.backup_present = True
        check = gameversion.VersionCheck(status.installed, None, skse=gameversion.SkseCheck([
            gameversion.SkseFile(Path("skse64_1_5_97.dll"), status.skse_runtime, "game folder")
        ]))
        with patch.object(service, "game_version_check", return_value=check):
            card._on_game_status(status)
        text = card._label.text()
        self.assertIn("SKSE needs Skyrim 1.5.97", text)
        self.assertIn("Update Skyrim in Steam first", text)
        self.assertIn("Restore original files", text)
        self.assertNotIn(".dll", text)
        self.assertNotIn("runtime", text)
        self.assertLess(len(text.split()), 50)
        self.assertIn("skse64_1_5_97.dll", card._label.toolTip())

    def test_wrong_skse_offers_the_matching_build_or_the_download_page(self):
        service, card = self._card()
        version = gameversion.GameVersion.parse
        status = fake_game_status(service)
        status.installed = version("1.6.1170")
        status.expected = version("1.6.1170")
        status.skse_runtime = version("1.5.97")
        check = gameversion.VersionCheck(status.installed, status.expected)
        with patch.object(service, "game_version_check", return_value=check):
            card._on_game_status(status)
        self.assertTrue(card._skse.isVisibleTo(card))
        self.assertEqual(card._skse.text(), "Install SKSE 2.2.6")
        self.assertIn("built for Skyrim 1.5.97, not 1.6.1170", card._label.text())
        self.assertIn("Install SKSE 2.2.6", card._label.text())

        # The current Steam build's SKSE is Nexus-only: point there instead.
        status.installed = status.expected = version("1.7.104")
        status.skse_runtime = None
        with patch.object(service, "game_version_check", return_value=check):
            card._on_game_status(status)
        self.assertEqual(card._skse.text(), "Get SKSE…")
        self.assertIn("nexusmods.com", card._label.text())
        with patch("modsync.ui.game_card.QDesktopServices.openUrl") as open_url:
            card._install_skse()
        self.assertIn("nexusmods.com", open_url.call_args.args[0].toString())

        # Matching SKSE: nothing to offer.
        status.installed = status.expected = status.skse_runtime = version("1.6.1170")
        with patch.object(service, "game_version_check", return_value=check):
            card._on_game_status(status)
        self.assertFalse(card._skse.isVisibleTo(card))

    def test_unsupported_target_and_active_update_do_not_suggest_updating_to_downgrade(self):
        service, card = self._card()
        version = gameversion.GameVersion.parse
        status = fake_game_status(service)
        status.installed = version("1.6.1170")
        status.expected = version("1.5.97")
        # The available patch does not reach the requested version.
        check = gameversion.VersionCheck(status.installed, status.expected)
        with patch.object(service, "game_version_check", return_value=check):
            card._on_game_status(status)
            self.assertIn("can't switch this install to 1.5.97 yet", card._label.text())
            self.assertNotIn("Update Skyrim in Steam first", card._label.text())
            status.steam_updating = True
            card._on_game_status(status)
            self.assertIn("Wait for it to finish", card._label.text())
            self.assertNotIn("Some mods may not work", card._label.text())
            self.assertNotIn("return here to switch", card._label.text())


class DashboardLaunchTests(_SmokeBase):
    def test_launch_buttons_route_actions_and_recover_after_failure(self):
        from modsync.ui.dashboard import Dashboard
        service = ModSyncService(manager=object())
        service.state.instance_path = "/example/MO2"
        dash = Dashboard(service)
        self.addCleanup(dash.shutdown)
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        with patch("modsync.ui.dashboard.run_async") as run:
            dash._play_button.click()
            self.assertTrue(run.call_args.kwargs["play"])
            self.assertTrue(dash.busy)
            self.assertFalse(dash._open_mo2_button.isEnabled())
            self.assertFalse(dash.game.isEnabled())
            dash._on_launch_failed("Start Steam first")
            self.assertFalse(dash.busy)
            self.assertTrue(dash._play_button.isEnabled())
            self.assertIn("Start Steam first", dash._status_line.text())
            dash._open_mo2_button.click()
            self.assertFalse(run.call_args.kwargs["play"])
            dash._on_launched("Starting MO2")
        with patch.object(service.launcher, "running", return_value=True):
            dash._poll_launch()
            self.assertFalse(dash._play_button.isEnabled())
            self.assertFalse(dash._open_mo2_button.isEnabled())
            self.assertFalse(dash.game.isEnabled())
        dash._poll_launch()
        self.assertTrue(dash._play_button.isEnabled())
        self.assertTrue(dash.game.isEnabled())

    def test_play_requires_chosen_instance(self):
        from modsync.ui.dashboard import Dashboard
        with patch.object(Dashboard, "_scan_instances", return_value=[]):
            dash = Dashboard(ModSyncService(manager=object()))
            self.addCleanup(dash.shutdown)
            QThreadPool.globalInstance().waitForDone(5000)
            self.app.processEvents()
        self.assertFalse(dash._play_button.isEnabled())
        self.assertIsNone(dash._open_mo2_button)
