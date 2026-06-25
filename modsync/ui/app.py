"""GUI entry point. Kept tiny so `cli.launch_gui` can lazy-import it."""

from __future__ import annotations

import sys


def run(argv: list[str] | None = None) -> int:
    from PySide6.QtWidgets import QApplication

    from modsync.ui.main_window import MainWindow

    args = [sys.argv[0], *(argv or [])]
    app = QApplication.instance() or QApplication(args)
    app.setApplicationName("ModSync")
    app.setApplicationDisplayName("ModSync")

    window = MainWindow()
    # Gaming Mode (gamescope) renders non-maximized windows tiny/low-res, so we
    # always maximize ourselves.
    window.showMaximized()

    return app.exec()
