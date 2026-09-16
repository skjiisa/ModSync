"""The dashboard — always the app's home screen, laid out as independent cards.

* **Game**: the installed Skyrim runtime, whether it matches this setup, and the
  downgrade / pin / re-record buttons. Works with Steam alone.
* **Mod Organizer 2**: the instance this machine uses — choose, install, open.
* **Sync**: optional. Share the instance with another machine, or the live sync
  view once a vault exists.

Each card decides its own content from what is actually set up, so someone who
only wants the downgrader never sees a vault. "Reset setup…" forgets everything
without touching a single mod file.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modsync import background, platforms
from modsync.games import SKYRIM_SE
from modsync.mo2 import discover as mo2_discover
from modsync.service import ModSyncService
from modsync.steam import shortcuts
from modsync.ui.game_card import GameCard
from modsync.ui.sync_card import SyncCard
from modsync.ui.worker import run_async

_POLL_MS = 4000
_TAGLINE = f"Set up {SKYRIM_SE.name} for modding on this machine."


class Dashboard(QWidget):
    wizardRequested = Signal()  # user wants the linear wizard instead
    stateChanged = Signal()  # instance chosen/forgotten, vault created/joined/left -> rebuild

    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(14)
        outer.addLayout(self._build_header())

        # Cards scroll rather than clip: the Deck's 1280×800 is not much room
        # once the live sync view (QR code, devices) is open.
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)

        self.game = GameCard(service)
        self.game.status.connect(self._set_status)
        self.game.changed.connect(self._on_game_changed)
        self.mo2 = self._build_mo2_group()
        top = QHBoxLayout()
        top.setSpacing(18)
        top.addWidget(self.game, stretch=1)
        top.addWidget(self.mo2, stretch=1)
        body_layout.addLayout(top)

        self.sync = SyncCard(service)
        self.sync.status.connect(self._set_status)
        self.sync.stateChanged.connect(self.stateChanged.emit)
        body_layout.addWidget(self.sync, stretch=1 if self.sync.live else 0)
        body_layout.addWidget(self._build_integration_group())
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        self._status_line = QLabel("")
        self._status_line.setStyleSheet("color: palette(mid);")
        self._status_line.setWordWrap(True)
        outer.addWidget(self._status_line)

        self._bg_installed = False
        self._refresh_bg_status()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.game.poll)
        self._timer.timeout.connect(self.sync.poll)
        self._timer.timeout.connect(self._refresh_bg_status)
        self._timer.start(_POLL_MS)

    # --- header -------------------------------------------------------------
    def _build_header(self) -> QVBoxLayout:
        state = self.service.state
        col = QVBoxLayout()
        row = QHBoxLayout()
        title = QLabel(state.instance_label if state.has_instance else "ModSync")
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

        if state.has_instance:
            reset = QPushButton("Reset setup…")
            reset.setToolTip("Forget the instance and any sync on this machine. Mods are not deleted.")
            reset.clicked.connect(self._reset)
            row.addWidget(reset)
        col.addLayout(row)

        subtitle = QLabel(state.instance_path if state.has_instance else _TAGLINE)
        subtitle.setStyleSheet("color: palette(mid);")
        subtitle.setWordWrap(True)
        col.addWidget(subtitle)
        return col

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self,
            "Reset setup",
            "Start over on this machine?\n\n"
            "This forgets the chosen instance, stops any syncing and clears "
            "ModSync's setup so you can set it up differently.\n\n"
            "Your mods, downloads and profiles are NOT deleted — every file stays "
            "on disk.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.shutdown()
        self._set_status("Resetting…")
        run_async(
            self.service.reset,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    # --- Mod Organizer 2 card ------------------------------------------------
    def _build_mo2_group(self) -> QGroupBox:
        box = QGroupBox("Mod Organizer 2")
        v = QVBoxLayout(box)
        state = self.service.state
        if state.has_instance:
            path = QLabel(f"✅  {state.instance_path}")
            path.setWordWrap(True)
            v.addWidget(path)
            row = QHBoxLayout()
            open_folder = QPushButton("Open instance folder")
            open_folder.clicked.connect(self._open_folder)
            row.addWidget(open_folder)
            if not state.syncing:
                change = QPushButton("Change…")
                change.setToolTip("Use a different MO2 instance folder on this machine")
                change.clicked.connect(self._choose_instance)
                row.addWidget(change)
            else:
                note = QLabel("shared through the vault below")
                note.setStyleSheet("color: palette(mid);")
                row.addWidget(note)
            row.addStretch(1)
            v.addLayout(row)
        else:
            intro = QLabel(
                "No Mod Organizer 2 instance chosen yet. Pick the folder of an existing "
                "portable instance, or install a fresh one."
            )
            intro.setWordWrap(True)
            v.addWidget(intro)
            row = QHBoxLayout()
            choose = QPushButton("Choose folder…")
            choose.clicked.connect(self._choose_instance)
            install = QPushButton("Install MO2…")
            install.setToolTip("Guided install — opens the wizard, which streams the installer log")
            install.clicked.connect(self.wizardRequested.emit)
            row.addWidget(choose)
            row.addWidget(install)
            row.addStretch(1)
            v.addLayout(row)
            self._found_box = QVBoxLayout()
            v.addLayout(self._found_box)
            run_async(self._scan_instances, on_done=self._on_instances_found, on_failed=lambda _: None)
        v.addStretch(1)
        return box

    @staticmethod
    def _scan_instances() -> list[str]:
        plat = platforms.current()
        found = mo2_discover.discover_instances(plat.mo2_broad_roots(), plat.mo2_known_roots())
        return [str(p) for p in found]

    def _on_instances_found(self, paths: list[str]) -> None:
        if not paths or self.service.state.has_instance:
            return
        self._found_box.addWidget(QLabel("Found on this machine:"))
        for path in paths[:4]:
            row = QHBoxLayout()
            label = QLabel(path)
            label.setWordWrap(True)
            use = QPushButton("Use")
            use.clicked.connect(lambda _=False, p=path: self._use_instance(p))
            row.addWidget(label, stretch=1)
            row.addWidget(use)
            self._found_box.addLayout(row)

    def _choose_instance(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select your Mod Organizer 2 instance folder")
        if folder:
            self._use_instance(folder)

    def _use_instance(self, path: str) -> None:
        self._set_status("Reading the instance…")
        run_async(
            self.service.choose_instance,
            path,
            on_done=lambda _: self.stateChanged.emit(),
            on_failed=self._on_error,
        )

    def _open_folder(self) -> None:
        path = self.service.state.instance_path
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # --- background service + Steam shortcut ---------------------------------
    def _build_integration_group(self) -> QGroupBox:
        box = QGroupBox("On this machine")
        v = QVBoxLayout(box)
        what = (
            "keeps syncing and applies a queued Steam pin the moment Steam exits"
            if self.service.state.syncing
            else "applies a queued Steam pin the moment Steam exits and notices when Steam updates the game"
        )
        row = QHBoxLayout()
        self._bg_button = QPushButton("Run in background")
        self._bg_button.setToolTip(
            f"Install a systemd --user service that runs at login and {what}, "
            "without the ModSync window."
        )
        self._bg_button.clicked.connect(self._toggle_bg)
        self._bg_status = QLabel("Background service: checking…")
        self._bg_status.setWordWrap(True)
        steam_button = QPushButton("Add to Steam")
        steam_button.setToolTip(
            "Add ModSync as a non-Steam game so it's launchable from Gaming Mode"
        )
        steam_button.clicked.connect(self._add_to_steam)
        row.addWidget(self._bg_button)
        row.addWidget(self._bg_status, stretch=1)
        row.addWidget(steam_button)
        v.addLayout(row)
        return box

    def _toggle_bg(self) -> None:
        self._bg_button.setEnabled(False)
        if self._bg_installed:
            run_async(
                background.uninstall,
                on_done=lambda _: self._after_bg("Background service turned off."),
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
        self._set_status(message)
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
            "Turn off background service" if self._bg_installed else "Run in background"
        )

    def _add_to_steam(self) -> None:
        if shortcuts.steam_is_running():
            self._set_status(
                "⚠ Close Steam first (it rewrites its shortcuts on exit), "
                "then click “Add to Steam” again."
            )
            return
        run_async(shortcuts.add_modsync_to_steam, on_done=self._on_steam_added, on_failed=self._on_error)

    def _on_steam_added(self, paths: list) -> None:
        if paths:
            self._set_status(
                f"Added ModSync to Steam ({len(paths)} user(s)). Start Steam to find it "
                "in your library / Gaming Mode."
            )
        else:
            self._set_status(
                "No Steam users found — is Steam installed and run at least once?"
            )

    # --- plumbing ------------------------------------------------------------
    def _on_game_changed(self) -> None:
        # A downgrade or a re-record changes nothing the other cards show right
        # now, but a synced record reaches other machines: nudge Syncthing.
        if self.service.state.syncing:
            run_async(self.service.rescan, on_failed=lambda _: None)

    def _set_status(self, message: str) -> None:
        self._status_line.setText(message)

    def _on_error(self, message: str) -> None:
        self._set_status(f"⚠ {message}")

    def shutdown(self) -> None:
        """Stop polling so no new worker jobs are queued during teardown."""
        self._timer.stop()
        self.sync.shutdown()
