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
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QBoxLayout,
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

from modsync import background, diagnostics, launchhook, platforms
from modsync.games import SKYRIM_SE
from modsync.mo2 import discover as mo2_discover
from modsync.service import ModSyncService
from modsync.steam import shortcuts
from modsync.ui.game_card import GameCard
from modsync.ui.sync_card import SyncCard
from modsync.ui.theme import role
from modsync.ui.worker import run_async

_POLL_MS = 4000
_TAGLINE = f"Set up {SKYRIM_SE.name} for modding on this machine."


class Dashboard(QWidget):
    installRequested = Signal()
    wizardRequested = Signal()  # user wants the linear wizard instead
    stateChanged = Signal()  # instance chosen/forgotten, vault created/joined/left -> rebuild

    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._launching = False

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
        top = self._top_cards = QBoxLayout(QBoxLayout.Direction.LeftToRight)
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

        footer = QHBoxLayout()
        self._status_line = QLabel("")
        role(self._status_line, "secondary")
        self._status_line.setWordWrap(True)
        footer.addWidget(self._status_line, stretch=1)
        self._diag_button = QPushButton("Copy diagnostics")
        self._diag_button.setToolTip(
            "Copy the doctor report and the recent ModSync logs to the clipboard for a bug report. "
            "Pairing codes and keys are redacted."
        )
        self._diag_button.clicked.connect(self._copy_diagnostics)
        footer.addWidget(self._diag_button)
        outer.addLayout(footer)

        self._bg_installed = False
        self._refresh_bg_status()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.game.poll)
        self._timer.timeout.connect(self.sync.poll)
        self._timer.timeout.connect(self._refresh_bg_status)
        self._timer.timeout.connect(self._poll_hook)
        self._timer.timeout.connect(self._poll_launch)
        self._timer.start(_POLL_MS)
        self.game.busyChanged.connect(self._on_busy)
        self._update_launch_buttons()

    def resizeEvent(self, event) -> None:
        self._top_cards.setDirection(
            QBoxLayout.Direction.TopToBottom if self.width() < 1000
            else QBoxLayout.Direction.LeftToRight
        )
        super().resizeEvent(event)

    # --- header -------------------------------------------------------------
    def _build_header(self) -> QVBoxLayout:
        state = self.service.state
        col = QVBoxLayout()
        row = QHBoxLayout()
        title = QLabel(state.instance_label if state.has_instance else "ModSync")
        role(title, "title")
        title.setWordWrap(True)
        row.addWidget(title)
        row.addStretch(1)
        self._play_button = QPushButton("▶  Play Skyrim")
        role(self._play_button, "primary")
        self._play_button.setMinimumHeight(48)
        self._play_button.setMinimumWidth(190)
        self._play_button.setEnabled(state.has_instance)
        self._play_button.setToolTip(
            "Launch Skyrim through the chosen MO2 instance and its selected profile. "
            "Uses SKSE when available. Choose an MO2 instance first."
        )
        self._play_button.clicked.connect(lambda: self._launch_mo2(play=True))
        row.addWidget(self._play_button)

        wizard = self._wizard_button = QPushButton("Setup wizard")
        wizard.setToolTip("Prefer a guided, step-by-step flow? Run the wizard instead.")
        wizard.clicked.connect(self.wizardRequested.emit)
        row.addWidget(wizard)

        self._reset_button = None
        if state.has_instance:
            reset = self._reset_button = QPushButton("Reset setup…")
            reset.setToolTip("Forget the instance and any sync on this machine. Mods are not deleted.")
            reset.clicked.connect(self._reset)
            row.addWidget(reset)
        col.addLayout(row)

        subtitle = QLabel(state.instance_path if state.has_instance else _TAGLINE)
        role(subtitle, "secondary")
        subtitle.setWordWrap(True)
        col.addWidget(subtitle)
        return col

    def _reset(self) -> None:
        if self.busy:
            return
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
        self._open_mo2_button = None
        if state.has_instance:
            path = QLabel(f"✅  {state.instance_path}")
            path.setWordWrap(True)
            v.addWidget(path)
            launch_row = QHBoxLayout()
            self._open_mo2_button = QPushButton("Open MO2")
            self._open_mo2_button.setToolTip("Open the chosen Mod Organizer 2 instance to manage mods and profiles")
            self._open_mo2_button.clicked.connect(lambda: self._launch_mo2(play=False))
            launch_row.addWidget(self._open_mo2_button)
            launch_note = QLabel("Play Skyrim uses MO2’s selected profile and SKSE when available.")
            launch_note.setWordWrap(True)
            role(launch_note, "secondary")
            launch_row.addWidget(launch_note, stretch=1)
            v.addLayout(launch_row)
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
                role(note, "secondary")
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
            install.setToolTip("Choose an install folder and set up Mod Organizer 2")
            install.clicked.connect(self.installRequested.emit)
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

    def _launch_mo2(self, *, play: bool) -> None:
        if self.busy:
            return
        self._launching = True
        self._update_launch_buttons()
        self._set_status("Starting Skyrim through MO2…" if play else "Opening MO2…")
        run_async(self.service.launch_mo2, play=play,
                  on_done=self._on_launched, on_failed=self._on_launch_failed)

    def _on_launched(self, message: str) -> None:
        self._launching = False
        self._set_status(message)
        self._update_launch_buttons()

    def _on_launch_failed(self, message: str) -> None:
        self._launching = False
        self._on_error(message)
        self._update_launch_buttons()

    def _poll_launch(self) -> None:
        for message in self.service.launcher.poll():
            self._on_error(message)
        self._update_launch_buttons()

    def _update_launch_buttons(self) -> None:
        ready = self.service.state.has_instance and not self.busy
        self._play_button.setEnabled(ready and not self.service.launcher.running(play=True))
        self._wizard_button.setEnabled(not self.busy)
        if self._reset_button is not None:
            self._reset_button.setEnabled(not self.busy)
        self.mo2.setEnabled(not self.busy)
        self.sync.setEnabled(not self.busy)
        if self._open_mo2_button is not None:
            self._open_mo2_button.setEnabled(ready and not self.service.launcher.running(play=False))
        # Do not rewrite game files while a launch from this app is alive.
        self.game.setEnabled(not self._launching and not self.service.launcher.running())

    # --- background service, Steam shortcut, launch hook -----------------------
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
        steam_button = QPushButton("Add ModSync shortcut to Steam")
        steam_button.setToolTip(
            "Add ModSync as a non-Steam shortcut in your Steam library, including Gaming Mode. "
            "Close Steam first."
        )
        steam_button.clicked.connect(self._add_to_steam)
        row.addWidget(self._bg_button)
        row.addWidget(self._bg_status, stretch=1)
        row.addWidget(steam_button)
        v.addLayout(row)

        # The launch hook: Play in Steam opens ModSync first, then hands the same
        # launch on to Mod Organizer 2 / the game. Steam's previous choice is
        # restored on turn-off.
        hook_row = QHBoxLayout()
        self._hook_button = QPushButton("Open ModSync before Skyrim")
        self._hook_button.setToolTip(
            "Register a small Steam compatibility tool for the game that opens this hub "
            "before every launch — mod setup, sync state, game-version and SKSE issues — "
            "with Continue and Cancel. Turning it off puts Steam's previous choice back."
        )
        self._hook_button.clicked.connect(self._toggle_hook)
        self._hook_status = QLabel("Steam launch: checking…")
        self._hook_status.setWordWrap(True)
        hook_row.addWidget(self._hook_button)
        hook_row.addWidget(self._hook_status, stretch=1)
        v.addLayout(hook_row)
        self._hook: launchhook.LaunchHookStatus | None = None
        self._refresh_hook_status()
        return box

    def _toggle_hook(self) -> None:
        self._hook_button.setEnabled(False)
        st = self._hook
        turning_off = bool(st and (st.installed or st.selected) and not (st.pending and st.pending.action == "select"))
        if turning_off:
            run_async(launchhook.disable, on_done=self._after_hook, on_failed=self._on_hook_failed)
        else:
            run_async(launchhook.enable, on_done=self._after_hook, on_failed=self._on_hook_failed)

    def _after_hook(self, message: str) -> None:
        self._hook_button.setEnabled(True)
        self._set_status(message)
        self._refresh_hook_status()

    def _on_hook_failed(self, message: str) -> None:
        self._hook_button.setEnabled(True)
        self._on_error(message)
        self._refresh_hook_status()

    def _refresh_hook_status(self) -> None:
        run_async(
            launchhook.status,
            on_done=self._on_hook_status,
            on_failed=lambda _: self._hook_status.setText("Steam launch: unknown"),
        )

    def _on_hook_status(self, st: launchhook.LaunchHookStatus) -> None:
        self._hook = st
        if st.enabled and not st.pending:
            head = "on"
        elif st.pending:
            head = "switching when Steam closes"
        elif st.installed or st.selected:
            head = "partly set up"
        else:
            head = "off"
        self._hook_status.setText(f"Steam launch: {head}\n{st.summary()}")
        if st.pending and st.pending.action == "select":
            self._hook_button.setText("Turn off")
        elif st.installed or st.selected:
            self._hook_button.setText("Turn off" if st.enabled else "Turn off / reset")
        else:
            self._hook_button.setText("Open ModSync before Skyrim")
        self._hook_button.setEnabled(st.steam_found)

    def _poll_hook(self) -> None:
        """Timer: apply a queued launcher switch once Steam has exited."""
        if self._hook is None or self._hook.pending is None:
            return
        run_async(launchhook.apply_pending, on_done=self._on_hook_applied, on_failed=lambda _: None)

    def _on_hook_applied(self, message: object) -> None:
        if message:
            self._set_status(str(message))
            self._refresh_hook_status()

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
                "then click “Add ModSync shortcut to Steam” again."
            )
            return
        run_async(shortcuts.add_modsync_to_steam, on_done=self._on_steam_added, on_failed=self._on_error)

    def _on_steam_added(self, paths: list) -> None:
        if paths:
            self._set_status(
                f"Added a ModSync non-Steam shortcut ({len(paths)} user(s)). Start Steam to find it "
                "in your library / Gaming Mode."
            )
        else:
            self._set_status(
                "No Steam users found — is Steam installed and run at least once?"
            )

    # --- diagnostics -----------------------------------------------------------
    def _copy_diagnostics(self) -> None:
        self._diag_button.setEnabled(False)
        self._set_status("Collecting diagnostics…")
        run_async(diagnostics.build, on_done=self._on_diagnostics, on_failed=self._on_diagnostics_failed)

    def _on_diagnostics(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self._diag_button.setEnabled(True)
        self._set_status("Diagnostics copied to the clipboard — paste them into your bug report.")

    def _on_diagnostics_failed(self, message: str) -> None:
        self._diag_button.setEnabled(True)
        self._on_error(f"Could not collect diagnostics: {message}")

    # --- plumbing ------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.game.busy or self._launching

    def _on_busy(self, _busy: bool) -> None:
        self._update_launch_buttons()

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
