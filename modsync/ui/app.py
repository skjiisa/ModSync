"""GUI entry points. Kept tiny so `cli` can lazy-import them."""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def _install_excepthook() -> None:
    """Qt swallows exceptions raised in slots after printing them; make sure
    they also land in modsync.log, then let the previous hook have its say."""
    previous = sys.excepthook
    if getattr(previous, "_modsync", False):
        return

    def hook(exc_type, exc, tb):
        log.error("uncaught exception in the GUI", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    hook._modsync = True  # type: ignore[attr-defined]
    sys.excepthook = hook


def _application(argv: list[str] | None):
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from modsync.ui.theme import apply_theme

    _install_excepthook()
    args = [sys.argv[0], *(argv or [])]
    app = QApplication.instance() or QApplication(args)
    apply_theme(app)
    if not app.property("modsyncThemeConnected"):
        app.styleHints().colorSchemeChanged.connect(lambda _: apply_theme(app))
        app.setProperty("modsyncThemeConnected", True)
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
    from modsync.logging_setup import configure
    from modsync.ui.main_window import MainWindow

    configure("gui", argv)
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
    from modsync.logging_setup import configure
    from modsync.service import ModSyncService
    from modsync.ui.launch_hub import LaunchHub

    configure("hub", ["launch", "hub", f"--appid={appid}", f"--through={through}"])
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
    result = hub.decision if hub.decision is not None else (
        code if code in (launchhook.EXIT_CONTINUE, launchhook.EXIT_CANCEL) else launchhook.EXIT_CANCEL
    )
    log.info("hub closed: %s (exit %d)", "cancel" if result == launchhook.EXIT_CANCEL else "continue", result)
    return result
