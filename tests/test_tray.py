"""The tray icon a desktop session shows while the background service is on."""

import unittest
import unittest.mock
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
        self.unit.write_text(background.unit_text())
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
        # Installing again rewrites the unit in place: only a watch on the file sees that.
        self.assertIn(str(self.unit), t.watcher.files())

    def test_it_quits_once_the_service_is_off(self):
        t = tray.Tray(self.app)
        self.unit.unlink()
        with patch.object(self.app, "quit") as quit:
            t.refresh()
        quit.assert_called_once()
        self.assertTrue(t.closed)

    def test_it_quits_once_the_service_runs_another_setup(self):
        import os

        t = tray.Tray(self.app)
        with patch.dict(os.environ, {"MODSYNC_HOME": "/home/me/other"}):
            self.unit.write_text(background.unit_text())  # installed again from another setup
        with patch.object(self.app, "quit") as quit:
            t.refresh()
        quit.assert_called_once()

    def test_a_window_opened_from_it_closes_first(self):
        # Inside the Flatpak it shares the tray's sandbox, which goes when the tray does.
        t = tray.Tray(self.app)
        child = unittest.mock.Mock()
        child.poll.return_value = None
        t.children.append(child)
        with patch.object(self.app, "quit") as quit, patch("PySide6.QtCore.QTimer.singleShot") as later:
            t.close()
            quit.assert_not_called()
            later.assert_called_once()
            child.poll.return_value = 0
            t.close()
        quit.assert_called_once()

    def test_turned_off_while_starting_it_does_not_linger(self):
        # quit() before exec() is ignored: run() must not reach exec().
        real = tray.Tray.__init__

        def init(t, app):
            self.unit.unlink()  # turned off between run()'s check and the watcher
            real(t, app)

        with patch.object(tray.Tray, "__init__", init), patch.object(self.app, "exec") as exec_:
            self.assertEqual(tray.run(), 0)
        exec_.assert_not_called()

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

    def test_open_starts_the_app_from_this_process(self):
        import subprocess
        import sys

        t = tray.Tray(self.app)
        with patch.object(subprocess, "Popen") as popen:
            t.open_app()
        self.assertEqual(popen.call_args.args[0], [sys.executable, "-m", "modsync"])
        self.assertEqual(t.children, [popen.return_value])

    def test_one_tray_per_setup(self):
        import os

        first = tray._hold_lock()
        self.addCleanup(os.close, first)
        self.assertIsNotNone(first)
        self.assertIsNone(tray._hold_lock())


if __name__ == "__main__":
    unittest.main()
