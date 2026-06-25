"""The main window. For now a single-window dashboard showing the "this machine"
discovery report; the onboarding wizard and sync controls arrive in later phases.

Designed against Steam Deck Gaming-Mode constraints: single window, self-maximizing,
no system tray, standard input widgets only.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modsync import __version__


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"ModSync {__version__}")
        self.resize(1100, 720)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(12)

        title = QLabel("ModSync")
        title_font = QFont()
        title_font.setPointSize(22)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)

        subtitle = QLabel("Sync your Mod Organizer 2 setup across machines")
        subtitle.setStyleSheet("color: palette(mid);")

        section = QLabel("This machine")
        section_font = QFont()
        section_font.setPointSize(13)
        section_font.setWeight(QFont.Weight.DemiBold)
        section.setFont(section_font)

        self.report_view = QPlainTextEdit()
        self.report_view.setReadOnly(True)
        self.report_view.setPlainText("Scanning this machine…")
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.report_view.setFont(mono)

        buttons = QHBoxLayout()
        self.rescan_btn = QPushButton("Rescan")
        self.rescan_btn.clicked.connect(self.refresh)
        self.setup_btn = QPushButton("Set up sync…")
        self.setup_btn.setEnabled(False)
        self.setup_btn.setToolTip("Coming in a later phase (Syncthing setup).")
        buttons.addWidget(self.rescan_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.setup_btn)

        outer.addWidget(title)
        outer.addWidget(subtitle)
        outer.addSpacing(8)
        outer.addWidget(section)
        outer.addWidget(self.report_view, stretch=1)
        outer.addLayout(buttons)

        self.setCentralWidget(root)

        # Populate after the window paints so it appears instantly.
        QTimer.singleShot(0, self.refresh)

    def refresh(self) -> None:
        self.report_view.setPlainText("Scanning this machine…")
        # Defer the (blocking) filesystem walk so the label repaints first.
        # TODO: move discovery to a worker thread once the walk grows.
        QTimer.singleShot(0, self._do_scan)

    def _do_scan(self) -> None:
        from modsync.report import build

        try:
            report = build()
            self.report_view.setPlainText(report.text)
        except Exception as exc:  # defensive: never let a scan error blank the UI
            self.report_view.setPlainText(f"Discovery failed:\n{exc!r}")
