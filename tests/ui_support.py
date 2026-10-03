"""Shared set-up for the GUI tests: an offscreen QApplication, a throwaway
config directory, a service whose slow or system-touching calls are faked,
and a check that nothing raised inside a Qt slot or paint event (Qt only
prints those, so they would otherwise pass silently)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["MODSYNC_GAMEPAD"] = "0"

try:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - GUI extra not installed
    QApplication = None

from modsync import background, gameversion, launchhook
from modsync.firewall import Check
from modsync.games import SKYRIM_SE
from modsync.mo2 import usvfs
from modsync.service import GameStatus, ModSyncService, SyncStatus


def fake_game_status(self=None, *, refresh_index=True):
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
class UiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from modsync.ui.theme import apply_theme

        cls.app = QApplication.instance() or QApplication([])
        if not cls.app.styleSheet():
            apply_theme(cls.app)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        patches = [
            patch.object(ModSyncService, "game_status", fake_game_status),
            patch.object(ModSyncService, "game_version_check",
                         lambda self: gameversion.VersionCheck(gameversion.GameVersion.parse("1.7.104"), None)),
            patch.object(ModSyncService, "status", fake_sync_status),
            patch.object(ModSyncService, "my_pairing_code", lambda self: None),
            patch.object(ModSyncService, "accept_pending", lambda self: []),
            patch.object(ModSyncService, "prefix_runtime_problems", lambda self: []),
            patch.object(ModSyncService, "usvfs_status", lambda self: usvfs.Status("missing", "")),
            patch.object(background, "status", lambda: {"installed": False, "active": "inactive", "enabled": "disabled"}),
            patch.object(launchhook, "status", lambda appid=SKYRIM_SE.appid: launchhook.LaunchHookStatus(
                SKYRIM_SE, False, None, False, None, None, False, None, False, True)),
            patch("modsync.ui.pages.system.firewall.check", return_value=Check(None, True, "")),
            patch("modsync.ui.pages.mods.scan_instances", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.slot_errors = []
        previous = sys.excepthook
        sys.excepthook = lambda *exc: self.slot_errors.append(exc)
        self.addCleanup(setattr, sys, "excepthook", previous)
        self.addCleanup(self._no_slot_errors)
        from modsync.ui.input import InputRouter

        router = InputRouter.instance()
        router._set_mode("keyboard")
        # The router is app-wide: a press from the previous test must not count
        # as the duplicate of one in this test.
        router._recent.clear()
        router._held.clear()
        router._last_press = None

    def _no_slot_errors(self):
        self.settle()
        if self.slot_errors:
            import traceback

            text = "".join(traceback.format_exception(*self.slot_errors[0]))
            self.fail(f"exception inside a Qt slot or event handler:\n{text}")

    def settle(self, rounds: int = 2):
        for _ in range(rounds):
            QThreadPool.globalInstance().waitForDone(5000)
            self.app.processEvents()

    def window(self, *, steam=None, service=None, show=True):
        from modsync.ui.main_window import MainWindow

        window = MainWindow(steam_launch=steam, service=service or ModSyncService(manager=object()))
        window.resize(1280, 800)
        if show:
            window.show()
            window.activateWindow()
        self.settle()
        if show:
            window.focus_scope_default()
        self.addCleanup(self._close, window)
        return window

    def _close(self, window):
        from PySide6.QtCore import QCoreApplication, QEvent

        window._busy.clear()
        window.close()
        self.settle()
        # Delete it now: closed windows left alive pile up, and every later
        # style sheet change re-polishes all of their widgets.
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
