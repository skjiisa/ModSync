"""The **Sync** card — the optional part of ModSync.

Before the user opts in it is a short offer: share this instance from here
(create a vault) or copy another machine's (join). Once a vault exists it becomes
the live sync view: pairing code + QR, devices, folder progress, and "Stop
syncing", which leaves the vault but keeps the instance.
"""

from __future__ import annotations

import socket
import threading

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modsync import pairing_lan
from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService, SyncStatus
from modsync.ui.qr import pairing_pixmap
from modsync.ui.theme import role
from modsync.ui.worker import run_async


class SyncCard(QGroupBox):
    status = Signal(str)  # one-line messages for the host's status line
    stateChanged = Signal()  # vault created/joined/left -> host rebuilds

    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__("Sync with another machine", parent)
        self.service = service
        self.live = service.state.syncing

        self._announcements: list = []
        self._pairing = False
        self._pair_stop: threading.Event | None = None

        layout = QVBoxLayout(self)
        if self.live:
            self._build_live(layout)
            self._load_code()
            self.refresh()
            self._accept_pending()
        elif service.state.has_instance:
            self._build_offer(layout)
        else:
            hint = QLabel(
                "Optional. Once an instance is chosen you can keep it identical on "
                "another machine — desktop ↔ Steam Deck, for example."
            )
            hint.setWordWrap(True)
            role(hint, "secondary")
            layout.addWidget(hint)

    # --- offer (instance chosen, not syncing) --------------------------------
    def _build_offer(self, v: QVBoxLayout) -> None:
        intro = QLabel(
            "Optional. Keep this exact mod setup on another machine too — mods, load "
            "order and downloads stay identical (desktop ↔ Steam Deck), while each "
            "machine keeps its own game paths. Pick which side has the mods:"
        )
        intro.setWordWrap(True)
        v.addWidget(intro)
        row = QHBoxLayout()
        create = QPushButton("This machine has the mods")
        create.setToolTip("Share this instance: create a vault and get a pairing code for the other machine")
        create.clicked.connect(self._create_vault)
        self._join_btn = QPushButton("Copy from another machine…")
        self._join_btn.setToolTip("Join the machine that already has the setup (e.g. your Steam Deck)")
        self._join_btn.clicked.connect(self._toggle_join_panel)
        row.addWidget(create)
        row.addWidget(self._join_btn)
        row.addStretch(1)
        v.addLayout(row)
        self._join_panel = self._build_join_panel()
        self._join_panel.setVisible(False)
        v.addWidget(self._join_panel)

    def _build_join_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(18, 6, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Machines offering to pair:"))
        row.addStretch(1)
        self._scan_btn = QPushButton("Scan network")
        self._scan_btn.clicked.connect(self._scan)
        row.addWidget(self._scan_btn)
        v.addLayout(row)

        self._net_list = QListWidget()
        self._net_list.setMaximumHeight(110)
        v.addWidget(self._net_list)

        pin_row = QHBoxLayout()
        pin_row.addWidget(QLabel("PIN shown on that machine:"))
        self._pin_edit = QLineEdit()
        self._pin_edit.setMaxLength(7)
        self._pin_edit.setPlaceholderText("042 815")
        join_net = QPushButton("Join")
        join_net.clicked.connect(self._join_network)
        pin_row.addWidget(self._pin_edit, stretch=1)
        pin_row.addWidget(join_net)
        v.addLayout(pin_row)

        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("…or paste a pairing code:"))
        self._code_in = QLineEdit()
        self._code_in.setPlaceholderText("MODSYNC1-…")
        join_code = QPushButton("Join with code")
        join_code.clicked.connect(self._join_code)
        code_row.addWidget(self._code_in, stretch=1)
        code_row.addWidget(join_code)
        v.addLayout(code_row)
        return panel

    def _toggle_join_panel(self) -> None:
        visible = not self._join_panel.isVisible()
        self._join_panel.setVisible(visible)
        if visible:
            self._scan()

    def _scan(self) -> None:
        if not self._scan_btn.isEnabled():
            return
        self._announcements = []
        self._scan_btn.setEnabled(False)
        self._net_list.clear()
        self._net_list.addItem("Scanning…")
        run_async(
            pairing_lan.discover,
            on_done=self._on_scanned,
            on_failed=self._on_scan_failed,
            timeout=3.0,
        )

    def _on_scanned(self, anns: list) -> None:
        self._scan_btn.setEnabled(True)
        self._announcements = anns
        self._net_list.clear()
        if not anns:
            self._net_list.addItem(
                "No machines found — start “Pair over network” on the other machine, then Scan again."
            )
            return
        for a in anns:
            self._net_list.addItem(f"{a.name}   ({a.host})")

    def _on_scan_failed(self, message: str) -> None:
        self._announcements = []
        self._scan_btn.setEnabled(True)
        self._net_list.clear()
        self._net_list.addItem(f"Scan failed: {message}")

    def _create_vault(self) -> None:
        path = self.service.state.instance_path
        if not path:
            return
        self.status.emit("Creating a vault and starting Syncthing…")
        run_async(
            self.service.create_vault,
            path,
            self.service.state.instance_label,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _join_network(self) -> None:
        path = self.service.state.instance_path
        row = self._net_list.currentRow()
        if not path or not (0 <= row < len(self._announcements)):
            self._on_error("Pick a machine from the list first.")
            return
        pin = self._pin_edit.text().replace(" ", "").strip()
        if len(pin) != 6 or not pin.isdigit():
            self._on_error("Enter the 6-digit PIN shown on the other machine.")
            return
        self.status.emit("Pairing over the network…")
        run_async(
            self.service.join_via_network,
            self._announcements[row],
            pin,
            path,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _join_code(self) -> None:
        path = self.service.state.instance_path
        if not path:
            return
        try:
            code = PairingCode.decode(self._code_in.text())
        except Exception:
            self._on_error("That doesn't look like a valid pairing code.")
            return
        self.status.emit("Joining…")
        run_async(
            self.service.join_vault,
            code,
            path,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    # --- live (syncing) -------------------------------------------------------
    def _build_live(self, v: QVBoxLayout) -> None:
        self._folder_state = QLabel("starting Syncthing…")
        v.addWidget(self._folder_state)
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        v.addWidget(self._progress)

        row = QHBoxLayout()
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self._rescan)
        open_ui = QPushButton("Open Syncthing UI")
        open_ui.clicked.connect(self._open_ui)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        stop = QPushButton("Stop syncing…")
        stop.setToolTip("Leave the vault but keep using this instance here. Mods are not deleted.")
        stop.clicked.connect(self._stop_sync)
        row.addWidget(rescan)
        row.addWidget(open_ui)
        row.addWidget(refresh)
        row.addStretch(1)
        row.addWidget(stop)
        v.addLayout(row)

        columns = QHBoxLayout()
        columns.setSpacing(18)
        columns.addWidget(self._build_share_group(), stretch=1)
        columns.addWidget(self._build_devices_group(), stretch=1)
        v.addLayout(columns, stretch=1)

    def _build_share_group(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "On another machine, choose “Copy from another machine” — or paste this code:"
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

    def _build_devices_group(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Devices:"))
        self._devices = QListWidget()
        layout.addWidget(self._devices, stretch=1)
        btn_row = QHBoxLayout()
        self._pair_btn = QPushButton("Pair over network…")
        self._pair_btn.setToolTip("Show a PIN so another machine can find and join this one")
        self._pair_btn.clicked.connect(self._pair_network)
        add = QPushButton("Add code…")
        add.setToolTip("Add a machine by pasting its pairing code")
        add.clicked.connect(self._add_device)
        btn_row.addWidget(self._pair_btn)
        btn_row.addWidget(add)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)
        return box

    def poll(self) -> None:
        """Called by the host's timer while syncing."""
        if not self.live:
            return
        self.refresh()
        self._accept_pending()

    def _load_code(self) -> None:
        run_async(self.service.my_pairing_code, on_done=self._on_code, on_failed=self._on_error)

    def _on_code(self, code: PairingCode | None) -> None:
        if not code:
            return
        text = code.encode()
        self._code_edit.setText(text)
        self._code_edit.setCursorPosition(0)
        pixmap = pairing_pixmap(text)
        if pixmap is not None:
            self._qr.setPixmap(pixmap)
        else:
            self._qr.setText("(install 'qrcode' to show a QR code)")

    def refresh(self) -> None:
        if self.live:
            run_async(self.service.status, on_done=self._on_status, on_failed=self._on_error)

    def _on_status(self, status: SyncStatus) -> None:
        self._devices.clear()
        if not status.devices:
            self._devices.addItem("No other devices yet — pair one to start syncing.")
        for dev in status.devices:
            mark = "🟢 connected" if dev.connected else "⚪ offline"
            name = dev.name or dev.id[:13]
            self._devices.addItem(f"{name} — {mark}")

        state = status.folder_state or "starting"
        pct = int(round((status.completion or 0)))
        self._folder_state.setText(f"Folder: {state}   ·   {pct}% in sync")
        self._progress.setValue(max(0, min(100, pct)))

    def _accept_pending(self) -> None:
        # Auto-accept a machine that joined with our code, so pairing needs only
        # one code, one way.
        run_async(self.service.accept_pending, on_done=self._on_accepted, on_failed=lambda _: None)

    def _on_accepted(self, accepted: list) -> None:
        if accepted:
            n = len(accepted)
            self.status.emit(f"Paired with {n} new device{'' if n == 1 else 's'}.")
            self.refresh()

    # --- live: actions ---------------------------------------------------------
    def _copy_code(self) -> None:
        if self._code_edit.text():
            QGuiApplication.clipboard().setText(self._code_edit.text())
            self.status.emit("Pairing code copied to clipboard.")

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
            on_done=lambda _: (self.status.emit("Device added."), self.refresh()),
            on_failed=self._on_error,
        )

    def _pair_network(self) -> None:
        if self._pairing:  # doubles as Cancel while waiting
            if self._pair_stop is not None:
                self._pair_stop.set()
            self._end_pairing("Network pairing cancelled.")
            return
        pin = pairing_lan.make_pin()
        self._pair_stop = threading.Event()
        self._pairing = True
        self._pair_btn.setText("Cancel pairing")
        name = socket.gethostname() or "this machine"
        self.status.emit(
            "On the other machine choose “Copy from another machine”, then enter PIN "
            f"<b style='font-size:15pt'>{pin[:3]} {pin[3:]}</b>. Waiting…"
        )
        run_async(
            self.service.host_network_pairing,
            name,
            pin,
            stop=self._pair_stop,
            on_done=self._on_paired,
            on_failed=self._on_pair_failed,
        )

    def _on_paired(self, peer: object) -> None:
        if not self._pairing:
            return
        self._end_pairing("Paired with a new machine over the network!")
        self.refresh()

    def _on_pair_failed(self, message: str) -> None:
        if not self._pairing:  # already cancelled
            return
        self._end_pairing(f"⚠ Pairing: {message}")

    def _end_pairing(self, message: str) -> None:
        self._pairing = False
        self._pair_btn.setText("Pair over network…")
        self.status.emit(message)

    def _rescan(self) -> None:
        run_async(
            self.service.rescan,
            on_done=lambda _: self.status.emit("Rescan triggered."),
            on_failed=self._on_error,
        )

    def _open_ui(self) -> None:
        def url() -> str:
            self.service.ensure_running()
            return self.service.manager.base_url

        run_async(url, on_done=lambda u: QDesktopServices.openUrl(QUrl(u)), on_failed=self._on_error)

    def _stop_sync(self) -> None:
        answer = QMessageBox.question(
            self,
            "Stop syncing",
            "Leave the vault on this machine?\n\n"
            "This stops syncing and forgets the paired devices, but keeps using "
            "the instance here — you can share it again later.\n\n"
            "Your mods, downloads and profiles are NOT deleted — every file stays "
            "on disk.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.shutdown()
        self.status.emit("Stopping sync…")
        run_async(
            self.service.stop_sync,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _on_error(self, message: str) -> None:
        self.status.emit(f"⚠ {message}")

    def shutdown(self) -> None:
        """Unblock a waiting network-pairing worker before teardown."""
        if self._pairing and self._pair_stop is not None:
            self._pair_stop.set()
