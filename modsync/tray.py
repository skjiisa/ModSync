"""The tray icon (``modsync tray``) that a desktop session shows while the
background service is on, so it isn't left running and forgotten.

Started at each desktop login by the autostart entry ``modsync service
install`` adds (see modsync.background), and right away by the install. It
costs nothing while idle: it reads the state file only when that changes, and
never talks to Syncthing. It quits once the service is off, whoever turned it
off, or runs another copy or setup of ModSync, and at once where there is no
tray (Gaming Mode has none).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from modsync import background, config
from modsync.state import State

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Linux
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

LOCK = "tray.lock"  # in ModSync's data folder: one tray per setup
_TRAY_WAIT_MS = 30_000  # at login the panel may come up after this


def _synced_names(st: State) -> tuple[list[str], list[str]]:
    """What syncs here, and what is only kept here for other machines, by name."""
    from modsync.sync import folders

    def name(key: str) -> str:
        spec = folders.spec_for(key)
        return spec.label.removesuffix(" (ModSync)") if spec else key

    synced = [st.instance_label] if st.syncing else []
    synced += [name(k) for k in st.game_folders]
    return synced, [name(k) for k in st.held_folders]


def summary(st: State) -> list[str]:
    """The lines the tray shows under its title: what the service is doing."""
    synced, kept = _synced_names(st)
    lines = []
    if st.paused_for_play:
        lines.append("Paused while you play")
    elif st.copying:
        lines.append("Copying mods from another machine")
    if synced:
        lines.append("Syncing " + ", ".join(synced))
    if st.syncing and st.sync_paused:
        lines.append(f"{st.instance_label} is paused")
    if kept:
        lines.append("Keeping " + ", ".join(kept) + " for other machines")
    if not synced and not kept:
        lines.append("Waiting for another machine to offer a game" if st.paired
                     else "Nothing syncs here; it only watches Steam")
    return lines


def _hold_lock() -> int | None:
    """The tray's lock, or None when another tray of this setup holds it."""
    if fcntl is None:
        return -1
    path = config.data_dir() / LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _icon():
    from PySide6.QtGui import QIcon

    icon = QIcon.fromTheme(background.APP_ID)
    if icon.isNull():  # run from source, not installed: the bundled copy
        icon = QIcon(str(Path(__file__).parent / "ui" / "tray.png"))
    return icon


class Tray:
    """The icon, its tooltip and menu, kept to what the state file says."""

    def __init__(self, app) -> None:
        from PySide6.QtCore import QFileSystemWatcher
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon

        self.app = app
        self.closed = False
        self.children: list[subprocess.Popen] = []  # windows opened from here
        self.icon = QSystemTrayIcon(_icon())
        self.menu = QMenu()
        self.title = self.menu.addAction("ModSync is running in the background")
        self.title.setEnabled(False)
        self.lines: list = []
        self.status_end = self.menu.addSeparator()
        self.menu.addAction("Open ModSync", self.open_app)
        self.menu.addAction("Turn off background service…", self.turn_off)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        # Watch folders, not files: the state file is replaced on each write,
        # and the unit file is gone once the service is turned off.
        self.watcher = QFileSystemWatcher()
        for folder in (config.config_dir(), background.unit_path().parent):
            if folder.is_dir():
                self.watcher.addPath(str(folder))
        self.watcher.directoryChanged.connect(lambda _: self.refresh())
        # And the unit itself: installing again from another setup rewrites it
        # in place, which the folder's watch doesn't report.
        self.watcher.fileChanged.connect(lambda _: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        if self.closed:
            return
        if not background.installed_here():
            log.info("tray: the background service is off, or another setup's; quitting")
            self.close()
            return
        unit = str(background.unit_path())
        if unit not in self.watcher.files():  # a file replaced on disk drops out of the watch
            self.watcher.addPath(unit)
        try:
            lines = summary(State.load())
        except Exception as exc:  # a newer ModSync's state, say: still say it runs
            lines = [f"State unreadable: {exc}"]
        for action in self.lines:
            self.menu.removeAction(action)
        self.lines = []
        for line in lines:
            action = self.menu.addAction(line)
            action.setEnabled(False)
            self.menu.insertAction(self.status_end, action)
            self.lines.append(action)
        self.icon.setToolTip("\n".join(["ModSync is running in the background", *lines]))

    def _activated(self, reason) -> None:
        from PySide6.QtWidgets import QSystemTrayIcon

        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.open_app()

    def close(self) -> None:
        """Hide the icon and quit; once a window opened from here has closed,
        since inside the Flatpak it shares this sandbox and would go with it."""
        from PySide6.QtCore import QTimer

        self.closed = True
        self.icon.hide()
        self.children = [c for c in self.children if c.poll() is None]
        if self.children:
            QTimer.singleShot(2000, self.close)
        else:
            self.app.quit()

    def open_app(self) -> None:
        # From this process, not the host: here the display is this session's
        # for certain, and the environment carries the setup (MODSYNC_HOME).
        try:
            self.children.append(subprocess.Popen(
                [sys.executable, "-m", "modsync"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        except OSError as exc:
            self._warn(f"Could not open ModSync: {exc}")

    def turn_off(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        answer = QMessageBox.question(
            None, "ModSync",
            "Turn off ModSync's background service?\n\nSyncing then happens only while ModSync is open, "
            "and a queued Steam change waits for ModSync to be open when Steam closes. Nothing is "
            "deleted; you can turn it on again under Settings.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            background.uninstall()
        except Exception as exc:
            self._warn(str(exc))
            return
        self.refresh()  # quits

    def _warn(self, message: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning(None, "ModSync", message)


def run() -> int:
    from modsync.logging_setup import configure

    configure("tray", ["tray"])
    if not background.installed_here():
        log.info("tray: the background service is off, or runs another copy or setup")
        return 0
    lock = _hold_lock()
    if lock is None:
        log.info("tray: one is running already")
        return 0

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QSystemTrayIcon

    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setApplicationName("ModSync")
    app.setDesktopFileName(background.APP_ID)
    app.setQuitOnLastWindowClosed(False)  # the turn-off question is a window
    tray = Tray(app)
    if tray.closed:  # turned off meanwhile: quit() before exec() would be ignored
        return 0

    waited = 0

    def show() -> None:
        nonlocal waited
        if tray.closed:
            return
        if QSystemTrayIcon.isSystemTrayAvailable():
            tray.icon.show()
            return
        if waited >= _TRAY_WAIT_MS:
            log.info("tray: no system tray in this session")
            app.quit()
            return
        waited += 1000
        QTimer.singleShot(1000, show)

    show()
    return app.exec()
