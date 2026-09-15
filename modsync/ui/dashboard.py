"""The dashboard — always the app's home screen.

When nothing is set up yet it shows a **setup section** you can fill in right
here (choose an instance, then create or join a vault); the linear wizard stays
available as an option for anyone who prefers it. Once a vault exists, the live
sync view takes over: pairing code + QR, devices, folder progress, the
background/Steam integrations, and a **game version** check that warns when this
machine's Skyrim runtime differs from the one the vault was set up for. "Reset
setup…" undoes it all without touching a single mod file.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
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

from modsync import background, pairing_lan
from modsync.downgrade.engine import Progress
from modsync.pairing_code import PairingCode
from modsync.service import GameStatus, ModSyncService, PinOutcome, SyncStatus
from modsync.steam import shortcuts
from modsync.ui.qr import pairing_pixmap
from modsync.ui.worker import run_async

_POLL_MS = 4000


class _ProgressBridge(QObject):
    """Marshals engine progress callbacks from the worker thread to the UI."""

    progressed = Signal(object)


def _bold(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class Dashboard(QWidget):
    wizardRequested = Signal()  # user wants the linear wizard instead
    stateChanged = Signal()  # setup created/joined/reset -> rebuild the dashboard

    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.configured = bool(service.state.configured)

        self._pending_instance: str | None = None
        self._announcements: list = []
        self._pairing = False
        self._pair_stop: threading.Event | None = None
        self._bg_installed = False
        self._game: GameStatus | None = None
        self._downgrading = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(14)
        outer.addLayout(self._build_header())

        if self.configured:
            columns = QHBoxLayout()
            columns.setSpacing(18)
            columns.addWidget(self._build_share_group(), stretch=1)
            columns.addWidget(self._build_devices_group(), stretch=1)
            outer.addLayout(columns, stretch=1)
            outer.addWidget(self._build_status_group())
            outer.addWidget(self._build_game_version_group())
            outer.addLayout(self._build_buttons())
            outer.addLayout(self._build_integration_buttons())
        else:
            outer.addWidget(self._build_setup_group(), stretch=1)

        self._status_line = QLabel("")
        self._status_line.setStyleSheet("color: palette(mid);")
        self._status_line.setWordWrap(True)
        outer.addWidget(self._status_line)

        self._timer = QTimer(self)
        if self.configured:
            self._timer.timeout.connect(self.refresh)
            self._timer.timeout.connect(self._accept_pending)
            self._timer.timeout.connect(self._apply_pending_pin)
            self._timer.timeout.connect(self._refresh_bg_status)
            self._load_code()
            self._refresh_bg_status()
            self._refresh_game_version()
            self.refresh()
            self._accept_pending()
            self._timer.start(_POLL_MS)

    # --- header -------------------------------------------------------------
    def _build_header(self) -> QVBoxLayout:
        col = QVBoxLayout()
        row = QHBoxLayout()
        title = QLabel(self.service.state.instance_label if self.configured else "ModSync")
        tf = title.font()
        tf.setPointSize(20)
        tf.setBold(True)
        title.setFont(tf)
        row.addWidget(title)
        row.addStretch(1)

        wizard = QPushButton("Setup wizard")
        wizard.setToolTip("Prefer a guided, step-by-step flow? Run the wizard instead.")
        wizard.clicked.connect(self.wizardRequested.emit)
        row.addWidget(wizard)

        if self.configured:
            reset = QPushButton("Reset setup…")
            reset.setToolTip("Start over — stops syncing and clears setup. Mods are not deleted.")
            reset.clicked.connect(self._reset)
            row.addWidget(reset)
        col.addLayout(row)

        subtitle = QLabel(
            self.service.state.instance_path
            if self.configured
            else "Nothing is set up on this machine yet."
        )
        subtitle.setStyleSheet("color: palette(mid);")
        subtitle.setWordWrap(True)
        col.addWidget(subtitle)
        return col

    # --- setup (not configured yet) -----------------------------------------
    def _build_setup_group(self) -> QGroupBox:
        box = QGroupBox("Set up this machine")
        v = QVBoxLayout(box)
        intro = QLabel(
            "Two things to fill in. You can do them right here, or use the setup wizard."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)
        v.addSpacing(6)

        v.addWidget(_bold("1.  Mod Organizer 2 instance"))
        self._inst_label = QLabel()
        self._inst_label.setWordWrap(True)
        v.addWidget(self._inst_label)
        row1 = QHBoxLayout()
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self._choose_instance)
        install = QPushButton("Install MO2…")
        install.setToolTip("Guided install — opens the wizard, which streams the installer log")
        install.clicked.connect(self.wizardRequested.emit)
        row1.addWidget(choose)
        row1.addWidget(install)
        row1.addStretch(1)
        v.addLayout(row1)
        v.addSpacing(10)

        v.addWidget(_bold("2.  Sync vault"))
        self._vault_label = QLabel()
        self._vault_label.setWordWrap(True)
        v.addWidget(self._vault_label)
        row2 = QHBoxLayout()
        self._create_btn = QPushButton("Create a new vault")
        self._create_btn.setToolTip("This machine already has the mod setup I want to share")
        self._create_btn.clicked.connect(self._create_vault)
        self._join_btn = QPushButton("Join another machine…")
        self._join_btn.setToolTip("Copy the setup from a machine that already has it (e.g. your Steam Deck)")
        self._join_btn.clicked.connect(self._toggle_join_panel)
        row2.addWidget(self._create_btn)
        row2.addWidget(self._join_btn)
        row2.addStretch(1)
        v.addLayout(row2)

        self._join_panel = self._build_join_panel()
        self._join_panel.setVisible(False)
        v.addWidget(self._join_panel)

        v.addStretch(1)
        self._refresh_setup_state()
        return box

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

    def _instance_path(self) -> str | None:
        return self.service.state.instance_path or self._pending_instance

    def _refresh_setup_state(self) -> None:
        path = self._instance_path()
        self._inst_label.setText(f"✅  {path}" if path else "⚠  No instance chosen yet.")
        ready = bool(path)
        self._create_btn.setEnabled(ready)
        self._join_btn.setEnabled(ready)
        self._vault_label.setText(
            "⚠  Not set up. Create a vault if this machine has the mods — or join the "
            "machine that does (e.g. your Steam Deck) to copy them here."
            if ready
            else "Choose an instance above first."
        )

    def _choose_instance(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select your Mod Organizer 2 instance folder")
        if folder:
            self._pending_instance = folder
            self._refresh_setup_state()

    def _toggle_join_panel(self) -> None:
        visible = not self._join_panel.isVisible()
        self._join_panel.setVisible(visible)
        if visible:
            self._scan()

    def _scan(self) -> None:
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
        self._scan_btn.setEnabled(True)
        self._net_list.clear()
        self._net_list.addItem(f"Scan failed: {message}")

    def _create_vault(self) -> None:
        path = self._instance_path()
        if not path:
            return
        self._status_line.setText("Creating a vault and starting Syncthing…")
        run_async(
            self.service.create_vault,
            path,
            Path(path).name or "Mod Organizer 2",
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _join_network(self) -> None:
        path = self._instance_path()
        row = self._net_list.currentRow()
        if not path or not (0 <= row < len(self._announcements)):
            self._on_error("Pick a machine from the list first.")
            return
        pin = self._pin_edit.text().replace(" ", "").strip()
        if len(pin) != 6 or not pin.isdigit():
            self._on_error("Enter the 6-digit PIN shown on the other machine.")
            return
        self._status_line.setText("Pairing over the network…")
        run_async(
            self.service.join_via_network,
            self._announcements[row],
            pin,
            path,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _join_code(self) -> None:
        path = self._instance_path()
        if not path:
            self._on_error("Choose an instance folder first.")
            return
        try:
            code = PairingCode.decode(self._code_in.text())
        except Exception:
            self._on_error("That doesn't look like a valid pairing code.")
            return
        self._status_line.setText("Joining…")
        run_async(
            self.service.join_vault,
            code,
            path,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset setup",
            "Start over on this machine?\n\n"
            "This stops syncing and clears ModSync's setup so you can set it up "
            "differently (for example, join your Steam Deck instead of hosting).\n\n"
            "Your mods, downloads and profiles are NOT deleted — every file stays "
            "on disk.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._timer.stop()
        self._status_line.setText("Resetting…")
        run_async(
            self.service.reset,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    # --- configured: construction -------------------------------------------
    def _build_share_group(self) -> QGroupBox:
        box = QGroupBox("Share this machine")
        layout = QVBoxLayout(box)
        hint = QLabel(
            "On another machine, use “Join another machine” — or paste this code:"
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

    def _build_game_version_group(self) -> QGroupBox:
        box = QGroupBox("Game version")
        layout = QVBoxLayout(box)
        self._gv_label = QLabel("checking…")
        self._gv_label.setWordWrap(True)
        layout.addWidget(self._gv_label)
        row = QHBoxLayout()
        self._gv_adopt = QPushButton("Use this machine's version")
        self._gv_adopt.setToolTip(
            "Record the runtime installed here as the version this vault is built for. "
            "Do this after you upgrade or downgrade the game on purpose."
        )
        self._gv_adopt.clicked.connect(self._adopt_game_version)
        self._gv_adopt.setVisible(False)
        row.addWidget(self._gv_adopt)
        self._gv_downgrade = QPushButton("Downgrade…")
        self._gv_downgrade.setToolTip(
            "Rewrite the game files to the vault's version using community xdelta "
            "patches (Mulderland). Steam keeps launching the game normally."
        )
        self._gv_downgrade.clicked.connect(self._downgrade)
        self._gv_downgrade.setVisible(False)
        row.addWidget(self._gv_downgrade)
        self._gv_pin = QPushButton("Keep this version")
        self._gv_pin.setToolTip(
            "Steam wants to update the game. Pin the installed files so Steam treats "
            "them as current and launches without updating. Needs Steam closed; "
            "otherwise it is queued and applied when Steam restarts."
        )
        self._gv_pin.clicked.connect(self._pin)
        self._gv_pin.setVisible(False)
        row.addWidget(self._gv_pin)
        row.addStretch(1)
        layout.addLayout(row)
        self._gv_progress = QProgressBar()
        self._gv_progress.setRange(0, 100)
        self._gv_progress.setVisible(False)
        layout.addWidget(self._gv_progress)
        self._gv_progress_label = QLabel("")
        self._gv_progress_label.setStyleSheet("color: palette(mid);")
        self._gv_progress_label.setVisible(False)
        layout.addWidget(self._gv_progress_label)
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

    def _build_integration_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._bg_button = QPushButton("Run in background")
        self._bg_button.setToolTip(
            "Keep syncing via a systemd --user service after you close ModSync"
        )
        self._bg_button.clicked.connect(self._toggle_bg)
        self._bg_status = QLabel("Background service: checking…")
        self._bg_status.setWordWrap(True)
        self._steam_button = QPushButton("Add to Steam")
        self._steam_button.setToolTip(
            "Add ModSync as a non-Steam game so it's launchable from Gaming Mode"
        )
        self._steam_button.clicked.connect(self._add_to_steam)
        row.addWidget(self._bg_button)
        row.addWidget(self._bg_status)
        row.addWidget(self._steam_button)
        row.addStretch(1)
        return row

    # --- configured: data flow ----------------------------------------------
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
            self._devices.addItem("No other devices yet — pair one to start syncing.")
        for dev in status.devices:
            mark = "🟢 connected" if dev.connected else "⚪ offline"
            name = dev.name or dev.id[:13]
            self._devices.addItem(f"{name} — {mark}")

        state = status.folder_state or "starting"
        pct = int(round((status.completion or 0)))
        self._folder_state.setText(f"Folder: {state}   ·   {pct}% in sync")
        self._progress.setValue(max(0, min(100, pct)))

    def _refresh_game_version(self) -> None:
        run_async(
            self.service.game_status,
            on_done=self._on_game_status,
            on_failed=lambda m: self._gv_label.setText(f"⚠ Version check failed: {m}"),
        )

    def _on_game_status(self, st: GameStatus) -> None:
        self._game = st
        vc = self.service.game_version_check()  # local and cheap; reuses the wording
        lines = []
        if vc.mismatch:
            lines.append(f"⚠  {vc.summary()}")
            self._gv_label.setStyleSheet("color: palette(text);")
        elif vc.ok:
            lines.append(f"✅  {vc.summary()}")
            self._gv_label.setStyleSheet("color: palette(mid);")
        else:
            lines.append(f"•  {vc.summary()}")
            self._gv_label.setStyleSheet("color: palette(mid);")
        if st.needs_pin:
            lines.append(
                "⚠  Steam wants to update the game on its next launch. “Keep this version” "
                "makes Steam treat the installed files as current."
            )
        if st.pending_pin:
            lines.append("•  A pin is queued; it applies automatically the next time Steam is closed.")
        if st.mismatch and st.suggested_target is None and st.installed is not None and st.recipe_from:
            if str(st.installed) != st.recipe_from:
                lines.append(
                    f"•  Downgrade recipes currently start from {st.recipe_from}; let Steam "
                    f"update the game first, then downgrade to {st.expected}."
                )
            elif st.expected is not None:
                lines.append(f"•  No recipe reaches {st.expected} yet (targets: {', '.join(st.recipe_targets)}).")
        self._gv_label.setText("\n".join(lines))
        # Offer to (re)record only when there is something to record and it
        # would change what the vault says.
        self._gv_adopt.setVisible(vc.installed is not None and not vc.ok and not self._downgrading)
        target = st.suggested_target
        self._gv_downgrade.setVisible(target is not None and not self._downgrading)
        if target:
            self._gv_downgrade.setText(f"Downgrade to {target}…")
        self._gv_pin.setVisible(st.needs_pin and not st.pending_pin and not self._downgrading)

    def _downgrade(self) -> None:
        st = self._game
        target = st.suggested_target if st else None
        if not st or not target:
            return
        deck_note = (
            "• On Steam Deck, 1.6.x brings back the on-screen keyboard crash; the "
            "“Steam Deck Keyboard Fix for Skyrim” SKSE plugin works around it.\n"
            if target.startswith(("1.6.", "1.5."))
            else ""
        )
        answer = QMessageBox.question(
            self,
            f"Downgrade Skyrim to {target}",
            f"This rewrites the Skyrim files in Steam's folder from {st.installed} to {target} "
            "using xdelta patches published by Mulderland (open source, checksummed).\n\n"
            "• Roughly 1 GB is downloaded and kept for next time.\n"
            "• Steam keeps launching the game normally afterwards.\n"
            "• To go back to the current version, use “Verify integrity of game files” in Steam.\n"
            f"{deck_note}\nProceed?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._downgrading = True
        for btn in (self._gv_downgrade, self._gv_adopt, self._gv_pin):
            btn.setVisible(False)
        self._gv_progress.setVisible(True)
        self._gv_progress_label.setVisible(True)
        self._gv_progress_label.setText("Starting…")
        bridge = _ProgressBridge(self)
        bridge.progressed.connect(self._on_downgrade_progress)
        run_async(
            self.service.run_downgrade,
            target,
            lambda p: bridge.progressed.emit(p),
            on_done=self._on_downgraded,
            on_failed=self._on_downgrade_failed,
        )

    def _on_downgrade_progress(self, p: Progress) -> None:
        if p.stage == "download" and p.total:
            self._gv_progress.setRange(0, 100)
            self._gv_progress.setValue(int(100 * (p.done or 0) / p.total))
            self._gv_progress_label.setText(
                f"Downloading {p.message}: {(p.done or 0) / 1e6:,.0f} / {p.total / 1e6:,.0f} MB"
            )
        elif p.total:
            self._gv_progress.setRange(0, p.total)
            self._gv_progress.setValue(p.done or 0)
            self._gv_progress_label.setText(f"{p.stage.capitalize()}: {p.message}")
        else:
            self._gv_progress.setRange(0, 0)
            self._gv_progress_label.setText(f"{p.stage.capitalize()}: {p.message}")

    def _end_downgrade(self) -> None:
        self._downgrading = False
        self._gv_progress.setVisible(False)
        self._gv_progress_label.setVisible(False)
        self._refresh_game_version()

    def _on_downgraded(self, result: object) -> None:
        self._end_downgrade()
        version = getattr(result, "installed_version", "?")
        notes = " ".join(getattr(result, "notes", []) or [])
        self._status_line.setText(f"Downgrade complete — the game now reports {version}. {notes}".strip())

    def _on_downgrade_failed(self, message: str) -> None:
        self._end_downgrade()
        self._on_error(f"Downgrade failed: {message}")

    def _pin(self) -> None:
        run_async(self.service.pin_game_version, on_done=self._on_pinned, on_failed=self._on_error)

    def _on_pinned(self, out: PinOutcome) -> None:
        self._status_line.setText(out.message)
        self._refresh_game_version()

    def _apply_pending_pin(self) -> None:
        if self._game is None or not self._game.pending_pin:
            return
        run_async(self.service.apply_pending_pin, on_done=self._on_pending_pin_applied, on_failed=lambda _: None)

    def _on_pending_pin_applied(self, out: object) -> None:
        if out is not None:
            self._status_line.setText(getattr(out, "message", "Pin applied."))
            self._refresh_game_version()

    def _adopt_game_version(self) -> None:
        run_async(
            self.service.adopt_local_game_version,
            on_done=self._on_game_version_adopted,
            on_failed=self._on_error,
        )

    def _on_game_version_adopted(self, meta: object) -> None:
        if meta is None:
            self._on_error("Could not detect the game version on this machine.")
            return
        self._status_line.setText(
            "Recorded this machine's game version as the vault's expected version. "
            "Other machines will be warned if theirs differs."
        )
        self._refresh_game_version()

    def _accept_pending(self) -> None:
        # Auto-accept a machine that joined with our code, so pairing needs only
        # one code, one way.
        run_async(self.service.accept_pending, on_done=self._on_accepted, on_failed=lambda _: None)

    def _on_accepted(self, accepted: list) -> None:
        if accepted:
            n = len(accepted)
            self._status_line.setText(f"Paired with {n} new device{'' if n == 1 else 's'}.")
            self.refresh()

    # --- configured: actions ------------------------------------------------
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
        self._status_line.setText(
            "On the other machine choose “Join another machine”, then enter PIN "
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
        self._status_line.setText(message)

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

        run_async(url, on_done=lambda u: QDesktopServices.openUrl(QUrl(u)), on_failed=self._on_error)

    def _toggle_bg(self) -> None:
        self._bg_button.setEnabled(False)
        if self._bg_installed:
            run_async(
                background.uninstall,
                on_done=lambda _: self._after_bg("Background sync turned off."),
                on_failed=self._on_bg_failed,
            )
        else:
            run_async(
                background.install,
                on_done=self._after_bg,
                on_failed=self._on_bg_failed,
            )

    def _after_bg(self, message: str) -> None:
        self._bg_button.setEnabled(True)
        self._status_line.setText(message)
        self._refresh_bg_status()

    def _on_bg_failed(self, message: str) -> None:
        self._bg_button.setEnabled(True)
        self._on_error(message)
        self._refresh_bg_status()

    def _refresh_bg_status(self) -> None:
        # Polled by the timer: fail into the label, not the shared status line.
        run_async(
            background.status,
            on_done=self._on_bg_status,
            on_failed=lambda _: self._bg_status.setText("Background service: unknown"),
        )

    def _on_bg_status(self, st: dict) -> None:
        self._bg_installed = bool(st.get("installed"))
        active = st.get("active", "unknown")
        if active == "active":
            label = "running"
        elif self._bg_installed:
            label = str(active)
        else:
            label = "off" if active == "inactive" else str(active)
        self._bg_status.setText(f"Background service: {label}")
        self._bg_status.setToolTip(f"Start at login: {st.get('enabled', 'unknown')}")
        self._bg_button.setText(
            "Turn off background sync" if self._bg_installed else "Run in background"
        )

    def _add_to_steam(self) -> None:
        if shortcuts.steam_is_running():
            self._status_line.setText(
                "⚠ Close Steam first (it rewrites its shortcuts on exit), "
                "then click “Add to Steam” again."
            )
            return
        run_async(shortcuts.add_modsync_to_steam, on_done=self._on_steam_added, on_failed=self._on_error)

    def _on_steam_added(self, paths: list) -> None:
        if paths:
            self._status_line.setText(
                f"Added ModSync to Steam ({len(paths)} user(s)). Start Steam to find it "
                "in your library / Gaming Mode."
            )
        else:
            self._status_line.setText(
                "No Steam users found — is Steam installed and run at least once?"
            )

    def _on_error(self, message: str) -> None:
        self._status_line.setText(f"⚠ {message}")

    def shutdown(self) -> None:
        """Stop polling so no new worker jobs are queued during teardown."""
        self._timer.stop()
        if self._pairing and self._pair_stop is not None:
            self._pair_stop.set()  # unblock the host_network_pairing worker
