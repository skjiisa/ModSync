"""Status dashboard shown once a vault is configured.

Shows this machine's pairing code (text + QR) to share with other machines, the
list of paired devices and their connection state, the folder sync progress, and
controls (rescan, add device, open the instance folder, open Syncthing's web UI).
Status is polled off the UI thread on a timer.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService, SyncStatus
from modsync.ui.qr import pairing_pixmap
from modsync.ui.worker import run_async

_POLL_MS = 4000


class Dashboard(QWidget):
    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(14)

        title = QLabel(service.state.instance_label or "Mod Organizer 2")
        tf = title.font()
        tf.setPointSize(20)
        tf.setBold(True)
        title.setFont(tf)
        subtitle = QLabel(service.state.instance_path or "")
        subtitle.setStyleSheet("color: palette(mid);")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        columns = QHBoxLayout()
        columns.setSpacing(18)
        columns.addWidget(self._build_share_group(), stretch=1)
        columns.addWidget(self._build_devices_group(), stretch=1)
        outer.addLayout(columns, stretch=1)

        outer.addWidget(self._build_status_group())
        outer.addLayout(self._build_buttons())

        self._status_line = QLabel("")
        self._status_line.setStyleSheet("color: palette(mid);")
        self._status_line.setWordWrap(True)
        outer.addWidget(self._status_line)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)

        self._load_code()
        self.refresh()
        self._timer.start(_POLL_MS)

    # --- construction helpers ---
    def _build_share_group(self) -> QGroupBox:
        box = QGroupBox("Share this machine")
        layout = QVBoxLayout(box)
        hint = QLabel(
            "On another machine, choose “Join an existing vault” and paste this code:"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self._code_edit = QLineEdit()
        self._code_edit.setReadOnly(True)
        self._code_edit.setPlaceholderText("starting…")
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy_code)
        row.addWidget(self._code_edit, stretch=1)
        row.addWidget(copy)
        layout.addLayout(row)

        self._qr = QLabel()
        self._qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr.setMinimumHeight(180)
        layout.addWidget(self._qr)
        return box

    def _build_devices_group(self) -> QGroupBox:
        box = QGroupBox("Devices")
        layout = QVBoxLayout(box)
        self._devices = QListWidget()
        layout.addWidget(self._devices, stretch=1)
        add = QPushButton("Add device…")
        add.clicked.connect(self._add_device)
        layout.addWidget(add, alignment=Qt.AlignmentFlag.AlignLeft)
        return box

    def _build_status_group(self) -> QGroupBox:
        box = QGroupBox("Sync status")
        layout = QVBoxLayout(box)
        self._folder_state = QLabel("starting Syncthing…")
        layout.addWidget(self._folder_state)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self._rescan)
        open_folder = QPushButton("Open instance folder")
        open_folder.clicked.connect(self._open_folder)
        open_ui = QPushButton("Open Syncthing UI")
        open_ui.clicked.connect(self._open_ui)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(rescan)
        row.addWidget(open_folder)
        row.addWidget(open_ui)
        row.addStretch(1)
        row.addWidget(refresh)
        return row

    # --- data flow ---
    def _load_code(self) -> None:
        run_async(self.service.my_pairing_code, on_done=self._on_code, on_failed=self._on_error)

    def _on_code(self, code: PairingCode | None) -> None:
        if not code:
            return
        text = code.encode()
        self._code_edit.setText(text)
        pixmap = pairing_pixmap(text)
        if pixmap is not None:
            self._qr.setPixmap(pixmap)
        else:
            self._qr.setText("(install 'qrcode' to show a QR code)")

    def refresh(self) -> None:
        run_async(self.service.status, on_done=self._on_status, on_failed=self._on_error)

    def _on_status(self, status: SyncStatus) -> None:
        self._devices.clear()
        if not status.devices:
            self._devices.addItem("No other devices yet — share your pairing code.")
        for dev in status.devices:
            mark = "🟢 connected" if dev.connected else "⚪ offline"
            name = dev.name or dev.id[:13]
            self._devices.addItem(f"{name} — {mark}")

        state = status.folder_state or "starting"
        pct = int(round((status.completion or 0)))
        self._folder_state.setText(f"Folder: {state}   ·   {pct}% in sync")
        self._progress.setValue(max(0, min(100, pct)))

    # --- actions ---
    def _copy_code(self) -> None:
        if self._code_edit.text():
            QGuiApplication.clipboard().setText(self._code_edit.text())
            self._status_line.setText("Pairing code copied to clipboard.")

    def _add_device(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Add device", "Paste the pairing code from the other machine:"
        )
        if not ok or not text.strip():
            return
        try:
            code = PairingCode.decode(text)
        except Exception:
            self._on_error("That doesn't look like a valid pairing code.")
            return
        run_async(
            self.service.add_peer,
            code,
            on_done=lambda _: (self._status_line.setText("Device added."), self.refresh()),
            on_failed=self._on_error,
        )

    def _rescan(self) -> None:
        run_async(
            self.service.rescan,
            on_done=lambda _: self._status_line.setText("Rescan triggered."),
            on_failed=self._on_error,
        )

    def _open_folder(self) -> None:
        path = self.service.state.instance_path
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _open_ui(self) -> None:
        def url() -> str:
            self.service.ensure_running()
            return self.service.manager.base_url

        run_async(
            url,
            on_done=lambda u: QDesktopServices.openUrl(QUrl(u)),
            on_failed=self._on_error,
        )

    def _on_error(self, message: str) -> None:
        self._status_line.setText(f"⚠ {message}")

    def shutdown(self) -> None:
        """Stop polling so no new worker jobs are queued during teardown."""
        self._timer.stop()
