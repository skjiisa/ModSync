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
    QDialog,
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

from modsync import firewall, pairing_lan
from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService, SyncStatus
from modsync.ui.pin_dialog import PinDialog, pin_font
from modsync.ui.qr import pairing_pixmap
from modsync.ui.theme import role
from modsync.ui.worker import run_async


class SyncCard(QGroupBox):
    status = Signal(str)  # one-line messages for the host's status line
    stateChanged = Signal()  # vault created/joined/left -> host rebuilds
    pairingReady = Signal(str)  # worker -> GUI: address we're announcing

    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__("Sync with another machine", parent)
        self.service = service
        self.live = service.state.syncing

        self._announcements: list = []
        self._pairing = False
        self._pair_stop: threading.Event | None = None
        self._firewall: firewall.Firewall | None = None
        self._fw_allowed = True

        self.pairingReady.connect(self._on_pairing_ready)

        layout = QVBoxLayout(self)
        if self.live:
            self._build_live(layout)
            layout.addWidget(self._build_firewall_banner())
            self._load_code()
            self.refresh()
            self._accept_pending()
        elif service.state.has_instance:
            self._build_offer(layout)
            layout.addWidget(self._build_firewall_banner())
        else:
            hint = QLabel(
                "Optional. Once an instance is chosen you can keep it identical on "
                "another machine — desktop ↔ Steam Deck, for example."
            )
            hint.setWordWrap(True)
            role(hint, "secondary")
            layout.addWidget(hint)

    # --- firewall banner (both views) ------------------------------------------
    def _build_firewall_banner(self) -> QWidget:
        """Warn when ufw/firewalld is on: it drops pairing and sync traffic until
        our ports are allowed, and nothing else in the UI would say why."""
        self._fw_banner = QWidget()
        self._fw_banner.setVisible(False)
        row = QHBoxLayout(self._fw_banner)
        row.setContentsMargins(0, 6, 0, 0)
        self._fw_label = QLabel()
        self._fw_label.setWordWrap(True)
        role(self._fw_label, "warning")
        row.addWidget(self._fw_label, stretch=1)
        self._fw_btn = QPushButton("Allow in firewall…")
        self._fw_btn.setToolTip("Adds the rules with pkexec — you'll be asked for your password")
        self._fw_btn.clicked.connect(self._allow_firewall)
        row.addWidget(self._fw_btn)
        run_async(
            firewall.check,
            self.service.state.firewall_rules_stamp,
            on_done=self._on_firewall_checked,
            on_failed=lambda _: None,
        )
        return self._fw_banner

    def _on_firewall_checked(self, chk: firewall.Check) -> None:
        self._firewall = chk.firewall
        self._fw_allowed = chk.allowed
        fw = chk.firewall
        if fw is None or chk.allowed:
            self._fw_banner.setVisible(False)
            return
        self._fw_label.setText(
            f"<b>{fw.kind} is on.</b> It blocks pairing and syncing until ModSync's "
            f"ports are allowed ({', '.join(f'{p}/{proto}' for p, proto, _ in firewall.PORTS)})."
        )
        self._fw_btn.setToolTip(
            "Runs with pkexec (you'll be asked for your password):\n"
            + firewall.manual_instructions(fw).replace(" && ", "\n")
        )
        self._fw_banner.setVisible(True)

    def _allow_firewall(self) -> None:
        if self._firewall is None:
            return
        self._fw_btn.setEnabled(False)
        self.status.emit(f"Adding ModSync's rules to {self._firewall.kind}…")
        run_async(
            firewall.allow,
            self._firewall,
            on_done=self._on_firewall_allowed,
            on_failed=self._on_firewall_failed,
        )

    def _on_firewall_allowed(self, stamp: object) -> None:
        self.service.state.firewall_rules_stamp = str(stamp or "")
        self.service.state.save()
        self._fw_allowed = True
        self._fw_banner.setVisible(False)
        self.status.emit(f"{self._firewall.kind}: ModSync's ports are now allowed.")

    def _on_firewall_failed(self, message: str) -> None:
        self._fw_btn.setEnabled(True)
        if "cancelled" in message:
            self.status.emit("Firewall unchanged.")
            return
        fw = self._firewall or firewall.Firewall("ufw")
        QMessageBox.warning(
            self,
            "Firewall",
            f"Could not change the firewall: {message}\n\nIn a terminal, run:\n\n"
            + firewall.manual_instructions(fw).replace(" && ", "\n"),
        )

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
        row.addWidget(QLabel("Pick the machine with the mods (it must be showing a PIN):"))
        row.addStretch(1)
        self._scan_btn = QPushButton("Scan network")
        self._scan_btn.clicked.connect(self._scan)
        row.addWidget(self._scan_btn)
        v.addLayout(row)

        # Picking a machine is the whole interaction: it opens the PIN dialog.
        self._net_list = QListWidget()
        self._net_list.setMaximumHeight(110)
        self._net_list.itemClicked.connect(self._on_machine_picked)
        self._net_list.itemActivated.connect(self._on_machine_picked)
        v.addWidget(self._net_list)

        # Broadcast discovery doesn't cross VLANs or isolated Wi-Fi clients, so
        # the other machine's address can be typed in instead of picked.
        alt = QHBoxLayout()
        addr_btn = QPushButton("Not listed? Enter its address…")
        addr_btn.setFlat(True)
        addr_btn.setToolTip("Pair with the IP address shown under “Pair over network” on that machine")
        addr_btn.clicked.connect(self._join_by_address)
        alt.addWidget(addr_btn)
        alt.addStretch(1)
        v.addLayout(alt)

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
            if self._firewall is not None and not self._fw_allowed:
                self._net_list.addItem(
                    f"{self._firewall.kind} is on here and drops their announcements — "
                    "use “Allow in firewall…” below, then Scan again."
                )
            else:
                self._net_list.addItem(pairing_lan.FIREWALL_HINT)
            return
        for a in anns:
            self._net_list.addItem(f"{a.name}   ({a.host})   — click to pair")

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

    def _on_machine_picked(self, item) -> None:
        row = self._net_list.row(item)
        if not (0 <= row < len(self._announcements)):  # placeholder / stale row
            return
        self._ask_pin(self._announcements[row])

    def _join_by_address(self) -> None:
        self._ask_pin(None)

    def _ask_pin(self, announcement: pairing_lan.Announcement | None) -> None:
        dlg = PinDialog(self, announcement)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            target = dlg.announcement()
        except pairing_lan.PairError as exc:
            self._on_error(str(exc))
            return
        self._join_network(target, dlg.pin())

    def _join_network(self, target: pairing_lan.Announcement, pin: str) -> None:
        path = self.service.state.instance_path
        if not path:
            return
        if len(pin) != 6 or not pin.isdigit():
            self._on_error("Enter the 6-digit PIN shown on the other machine.")
            return
        self.status.emit(f"Pairing with {target.name}…")
        run_async(
            self.service.join_via_network,
            target,
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

        # Normal view: pairing code + QR.
        self._share_normal = QWidget()
        nl = QVBoxLayout(self._share_normal)
        nl.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(
            "On another machine, choose “Copy from another machine” — or paste this code:"
        )
        hint.setWordWrap(True)
        nl.addWidget(hint)

        row = QHBoxLayout()
        self._code_edit = QLineEdit()
        self._code_edit.setReadOnly(True)
        self._code_edit.setPlaceholderText("starting…")
        copy = QPushButton("Copy")
        copy.clicked.connect(self._copy_code)
        row.addWidget(self._code_edit, stretch=1)
        row.addWidget(copy)
        nl.addLayout(row)

        self._qr = QLabel()
        self._qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr.setMinimumHeight(180)
        nl.addWidget(self._qr)
        layout.addWidget(self._share_normal)

        # While "Pair over network" is waiting, the same space shows the PIN —
        # big, so it's the one thing the joiner reads off this screen.
        self._pin_panel = QWidget()
        self._pin_panel.setVisible(False)
        pl = QVBoxLayout(self._pin_panel)
        pl.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Pairing PIN")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        role(title, "secondary")
        pl.addWidget(title)
        self._pin_label = QLabel()
        self._pin_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin_label.setFont(pin_font(self.font(), 36))
        self._pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pl.addWidget(self._pin_label)
        self._pin_steps = QLabel()
        self._pin_steps.setWordWrap(True)
        self._pin_steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pl.addWidget(self._pin_steps)
        self._pin_addr = QLabel()
        self._pin_addr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin_addr.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        role(self._pin_addr, "secondary")
        pl.addWidget(self._pin_addr)
        cancel_row = QHBoxLayout()
        cancel_row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self._pair_network)  # toggles off while pairing
        cancel_row.addWidget(cancel)
        cancel_row.addStretch(1)
        pl.addLayout(cancel_row)
        pl.addStretch(1)
        layout.addWidget(self._pin_panel)
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
        self._pin_label.setText(f"{pin[:3]} {pin[3:]}")
        self._pin_steps.setText(
            "On the other machine: <b>Copy from another machine</b> → pick "
            f"<b>{name}</b> from the list → enter this PIN."
        )
        self._pin_addr.setText("Starting…")
        self._share_normal.setVisible(False)
        self._pin_panel.setVisible(True)
        self.status.emit(f"Waiting for another machine to enter PIN {pin[:3]} {pin[3:]}…")

        def on_ready(ann: pairing_lan.Announcement) -> None:
            where = ann.host if ann.port == pairing_lan.PAIR_PORT else f"{ann.host}:{ann.port}"
            self.pairingReady.emit(where)

        run_async(
            self.service.host_network_pairing,
            name,
            pin,
            on_ready=on_ready,
            stop=self._pair_stop,
            on_done=self._on_paired,
            on_failed=self._on_pair_failed,
        )

    def _on_pairing_ready(self, where: str) -> None:
        if self._pairing:
            self._pin_addr.setText(f"Not listed there? Its address is {where}")

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
        self._pin_panel.setVisible(False)
        self._share_normal.setVisible(True)
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
