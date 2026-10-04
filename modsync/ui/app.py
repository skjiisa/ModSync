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


def _start_gamepads(app) -> None:
    """Read controllers directly (through SDL) as well as the keys Steam Input
    sends. Optional: without SDL the keyboard path still covers the Deck."""
    from modsync.ui.gamepad import Gamepads
    from modsync.ui.input import InputRouter

    pads = Gamepads(app)
    if pads.start():
        InputRouter.instance().attach(pads)
        app.aboutToQuit.connect(pads.stop)


def run(argv: list[str] | None = None) -> int:
    from modsync.logging_setup import configure
    from modsync.ui.main_window import MainWindow

    configure("gui", argv)
    app = _application(argv)
    _start_gamepads(app)
    window = MainWindow()
    # Gaming Mode (gamescope) renders non-maximized windows tiny/low-res, so we
    # always maximize ourselves.
    window.showMaximized()
    return app.exec()


def run_hub(*, appid: int | None = None, through: str | None = None) -> int:
    """The window the Steam launch hook opens: the regular one, with Steam's
    launch waiting on it. Returns the exit code the hook reads: 0 = continue the
    launch, 10 = cancel it."""
    from modsync import launchhook
    from modsync.games import GAMES, SKYRIM_SE
    from modsync.logging_setup import configure
    from modsync.ui.main_window import MainWindow

    configure("hub", ["launch", "hub", f"--appid={appid}", f"--through={through}"])
    app = _application(None)
    _start_gamepads(app)
    launch = launchhook.SteamLaunch(GAMES.get(appid or SKYRIM_SE.appid, SKYRIM_SE), through)
    window = MainWindow(steam_launch=launch)
    window.showMaximized()
    code = app.exec()  # the window's close stops polling, drains workers and its Syncthing
    result = launch.decision if launch.decision is not None else (
        code if code in (launchhook.EXIT_CONTINUE, launchhook.EXIT_CANCEL) else launchhook.EXIT_CANCEL
    )
    log.info("Steam launch: %s (exit %d)", "cancel" if result == launchhook.EXIT_CANCEL else "continue", result)
    return result
