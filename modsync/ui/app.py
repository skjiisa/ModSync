"""GUI entry points. Kept tiny so `cli` can lazy-import them."""

from __future__ import annotations

import sys


def _application(argv: list[str] | None):
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    args = [sys.argv[0], *(argv or [])]
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName("ModSync")
    app.setApplicationDisplayName("ModSync")
    # Lets Wayland compositors / taskbars match our windows to the installed
    # .desktop entry and its icon; the theme lookup covers X11 and the
    # non-Flatpak install (falls back to no icon if the theme lacks it).
    app.setDesktopFileName("io.github.skjiisa.ModSync")
    icon = QIcon.fromTheme("io.github.skjiisa.ModSync")
    if not icon.isNull():
        app.setWindowIcon(icon)
    return app


def run(argv: list[str] | None = None) -> int:
    from modsync.ui.main_window import MainWindow

    app = _application(argv)
    window = MainWindow()
    # Gaming Mode (gamescope) renders non-maximized windows tiny/low-res, so we
    # always maximize ourselves.
    window.showMaximized()
    return app.exec()


def run_hub(*, appid: int | None = None, through: str | None = None) -> int:
    """The pre-launch window the Steam launch hook opens. Returns the exit code
    the hook reads: 0 = continue the launch, 10 = cancel it."""
    from PySide6.QtCore import QThreadPool

    from modsync import launchhook
    from modsync.service import ModSyncService
    from modsync.ui.launch_hub import LaunchHub

    app = _application(None)
    service = ModSyncService()
    hub = LaunchHub(service, appid=appid, through=through)
    hub.showMaximized()
    code = app.exec()
    QThreadPool.globalInstance().waitForDone(5000)
    try:
        service.shutdown()  # stops a Syncthing the hub started; leaves the service's alone
    except Exception:
        pass
    if hub.decision is not None:
        return hub.decision
    return code if code in (launchhook.EXIT_CONTINUE, launchhook.EXIT_CANCEL) else launchhook.EXIT_CANCEL
