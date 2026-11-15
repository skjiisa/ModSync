"""The tray icon a desktop session shows while the background service is on."""

import unittest
from unittest.mock import patch

from tests import ui_support  # noqa: F401 - offscreen Qt

from modsync import background, tray
from modsync.state import State


class SummaryTests(unittest.TestCase):
    def test_what_syncs_and_what_is_kept(self):
        st = State(instance_path="/mo2", folder_id="modsync-1", instance_label="Skyrim SE",
                   game_folders={"dfu-mods": "/dfu"}, held_folders={"dfu-saves": "/kept"})
        self.assertEqual(tray.summary(st), ["Syncing Skyrim SE, Daggerfall Unity mods",
                                            "Keeping Daggerfall Unity saves for other machines"])

    def test_paused_for_a_game_or_by_the_user(self):
        st = State(instance_path="/mo2", folder_id="modsync-1", instance_label="Skyrim SE",
                   paused_for_play=["modsync-1"], sync_paused=True)
        self.assertEqual(tray.summary(st), ["Paused while you play", "Syncing Skyrim SE", "Skyrim SE is paused"])

    def test_nothing_to_sync_says_so(self):
        # The case the icon is for: on, but doing next to nothing.
        self.assertEqual(tray.summary(State()), ["Nothing syncs here; it only watches Steam"])
        self.assertEqual(tray.summary(State(paired=True)), ["Waiting for another machine to offer a game"])


class TrayTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from PySide6.QtWidgets import QApplication

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.unit = Path(tmp.name) / background.UNIT_NAME
        self.unit.write_text("service")
        p = patch.object(background, "unit_path", return_value=self.unit)
        p.start()
        self.addCleanup(p.stop)
        self.app = QApplication.instance() or QApplication([])

    def texts(self, t):
        return [a.text() for a in t.menu.actions() if not a.isSeparator()]

    def test_the_menu_follows_the_state(self):
        t = tray.Tray(self.app)
        self.assertEqual(self.texts(t), ["ModSync is running in the background",
                                         "Nothing syncs here; it only watches Steam",
                                         "Open ModSync", "Turn off background service…"])
        State.update(instance_path="/mo2", folder_id="modsync-1", instance_label="Skyrim SE")
        t.refresh()
        self.assertIn("Syncing Skyrim SE", self.texts(t))
        self.assertNotIn("Nothing syncs here; it only watches Steam", self.texts(t))
        self.assertIn("Syncing Skyrim SE", t.icon.toolTip())

    def test_it_quits_once_the_service_is_off(self):
        t = tray.Tray(self.app)
        self.unit.unlink()
        with patch.object(self.app, "quit") as quit:
            t.refresh()
        quit.assert_called_once()

    def test_turning_off_from_the_tray(self):
        from PySide6.QtWidgets import QMessageBox

        t = tray.Tray(self.app)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No), \
                patch.object(background, "uninstall") as uninstall:
            t.turn_off()
        uninstall.assert_not_called()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
                patch.object(background, "uninstall", side_effect=lambda: self.unit.unlink()), \
                patch.object(self.app, "quit") as quit:
            t.turn_off()
        quit.assert_called_once()

    def test_open_starts_the_app_detached(self):
        t = tray.Tray(self.app)
        with patch.object(background, "spawn_detached") as spawn:
            t.open_app()
        self.assertEqual(spawn.call_args.args[0], background.command())

    def test_one_tray_per_setup(self):
        import os

        first = tray._hold_lock()
        self.addCleanup(os.close, first)
        self.assertIsNotNone(first)
        self.assertIsNone(tray._hold_lock())


if __name__ == "__main__":
    unittest.main()
