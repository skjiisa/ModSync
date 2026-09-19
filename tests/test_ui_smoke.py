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
        self.assertIn("runtime here: 1.7.104", dash.game._label.text())
        self.assertIn("Steam launch: off", dash._hook_status.text())
        self.assertIn("Open ModSync when launching", dash._hook_button.text())

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
        wizard._on_next()  # choose -> game (commits the instance)
        self._settle()
        self.assertEqual(wizard._index, 2)
        self.assertEqual(State.load().instance_path, str(self.tmp))
        self.assertFalse(State.load().syncing)
        wizard._on_next()  # game -> sync
        self.assertEqual(wizard._vault.mode, "local")
        wizard._on_next()  # finish
        self.assertEqual(done[0]["mode"], "local")
        self.assertEqual(done[0]["instance_path"], str(self.tmp))

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


class LaunchHubSmokeTests(_SmokeBase):
    def _hub(self, through=None):
        from modsync.ui.launch_hub import LaunchHub

        hub = LaunchHub(ModSyncService(manager=object()), appid=489830, through=through)
        hub.resize(1120, 780)
        QThreadPool.globalInstance().waitForDone(5000)
        self.app.processEvents()
        return hub

    def test_labels_follow_the_underlying_tool(self):
        hub = self._hub(through="mo2_489830_redirector")
        self.assertEqual(hub.continue_button.text(), "Continue to Mod Organizer")
        self.assertTrue(hub.continue_button.isDefault())
        self.assertIn("No Mod Organizer 2 instance", hub._setup_label.text())
        self.assertIn("Not set up", hub._sync_label.text())
        hub2 = self._hub(through="GE-Proton10-34")
        self.assertEqual(hub2.continue_button.text(), "Continue to Skyrim Special Edition")

    def test_setup_summary_and_decisions(self):
        inst = self.tmp / "MO2"
        (inst / "profiles" / "Default").mkdir(parents=True)
        (inst / "mods").mkdir()
        (inst / "ModOrganizer.ini").write_text("[General]\ngameName=Skyrim Special Edition\nselected_profile=@ByteArray(Default)\n")
        (inst / "profiles/Default/modlist.txt").write_text("+SkyUI\n-Unused\n+USSEP\n")
        State(instance_path=str(inst), instance_label="My setup").save()
        hub = self._hub()
        self.assertIn("Profile: Default  ·  2 mods enabled", hub._setup_label.text())
        self.assertIn("Off — this machine's setup is not shared", hub._sync_label.text())
        hub.proceed()
        self.assertEqual(hub.decision, launchhook.EXIT_CONTINUE)
        hub.cancel()  # a second decision does not overwrite the first
        self.assertEqual(hub.decision, launchhook.EXIT_CONTINUE)
        other = self._hub()
        other.cancel()
        self.assertEqual(other.decision, launchhook.EXIT_CANCEL)
        closed = self._hub()
        closed.close()
        self.assertEqual(closed.decision, launchhook.EXIT_CANCEL)

    def test_syncing_shows_live_state(self):
        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        hub = self._hub()
        self.assertIn("In sync", hub._sync_label.text())
        hub._timer.stop()

    def test_auto_decision_env_is_a_testing_aid(self):
        from PySide6.QtTest import QTest

        with patch.dict(os.environ, {"MODSYNC_HUB_AUTO_DECISION": "cancel"}):
            hub = self._hub()
        self.assertIn("Test mode", hub._status_line.text())
        QTest.qWait(3500)
        self.assertEqual(hub.decision, launchhook.EXIT_CANCEL)
