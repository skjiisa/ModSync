"""The dashboard — always the app's home screen, laid out as independent cards.

* **Game**: the installed Skyrim runtime, whether it matches this setup, and the
  downgrade / pin / re-record buttons. Works with Steam alone.
* **Mod Organizer 2**: the instance this machine uses — choose, install, open.
* **Sync**: optional. Share the instance with another machine, or the live sync
  view once a vault exists.

Each card decides its own content from what is actually set up, so someone who
only wants the downgrader never sees a vault. "Reset setup…" forgets everything
without touching a single mod file.

When Steam's launch hook opened ModSync, Steam is waiting on this window. Play
then becomes **Continue**, which closes ModSync and lets the hook hand the same
launch on (to MO2-LINT's redirector or the game's Proton), and **Cancel launch**
returns to Steam. ModSync's own launches (Play through MO2, Open MO2) are left
out in that case: they would start a second Proton in the prefix next to the
one Steam is about to run, or outlive the launch Steam is tracking.
"""

from __future__ import annotations

from pathlib import Path

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

from modsync import background, diagnostics, firewall, launchhook, platforms, steamos
from modsync.games import SKYRIM_SE
from modsync.mo2 import discover as mo2_discover
from modsync.mo2 import instance as mo2_instance
from modsync.service import ModSyncService
from modsync.steam import shortcuts
from modsync.ui.game_card import GameCard
from modsync.ui.sync_card import SyncCard
from modsync.ui.theme import role
from modsync.ui.worker import run_async
from modsync.ui.usvfs_controls import UsvfsControls

_POLL_MS = 4000
_TAGLINE = f"Set up {SKYRIM_SE.name} for modding on this machine."


def describe_setup(instance_path: str | None) -> list[str]:
    """The selected profile, its enabled mod count and any instance problems.
    Cheap and file-system only."""
    if not instance_path or not Path(instance_path).is_dir():
        return []
    info = mo2_instance.inspect(instance_path)
    if not info.has_ini:
        return ["Mod Organizer 2 has not been started yet (no ModOrganizer.ini)."]
    enabled: int | None = None
    profiles_dir = info.content_dirs["profiles"].path if "profiles" in info.content_dirs else None
    if profiles_dir and info.selected_profile:
        modlist = profiles_dir / info.selected_profile / "modlist.txt"
        try:
            enabled = sum(1 for line in modlist.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("+"))
        except OSError:
            enabled = None
    detail = f"Profile: {info.selected_profile or '(none)'}"
    if enabled is not None:
        detail += f"  ·  {enabled} mods enabled"
    return [detail, *(f"⚠  {issue}" for issue in info.issues)]


class Dashboard(QWidget):
    installRequested = Signal()
    wizardRequested = Signal()  # user wants the linear wizard instead
    stateChanged = Signal()  # instance chosen/forgotten, vault created/joined/left -> rebuild
    launchDecided = Signal(int)  # Steam launch only: launchhook.EXIT_CONTINUE or EXIT_CANCEL

    def __init__(
        self,
        service: ModSyncService,
        parent: QWidget | None = None,
        *,
        steam_launch: launchhook.SteamLaunch | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.steam_launch = steam_launch
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

        # With Steam waiting to start the game, use the recipe index already on
        # disk rather than fetching the latest one.
        self.game = GameCard(service, refresh_index=steam_launch is None)
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
        self.sync.synced.connect(self.game.refresh)  # mods just arrived: re-check SKSE/version
        self.sync.progress.connect(self._on_sync_progress)
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
            "Copy the doctor report and recent ModSync logs to the clipboard for a bug report. "
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
        steam = self.steam_launch
        self._cancel_launch_button = None
        if steam is not None:
            cancel = self._cancel_launch_button = QPushButton("Cancel launch")
            cancel.setToolTip("Close ModSync and return to Steam without starting anything.")
            cancel.setMinimumHeight(48)
            cancel.clicked.connect(lambda: self.launchDecided.emit(launchhook.EXIT_CANCEL))
            row.addWidget(cancel)
        self._play_button = QPushButton(steam.continue_label if steam else "Play Skyrim")
        role(self._play_button, "primary")
        self._play_button.setMinimumHeight(48)
        self._play_button.setMinimumWidth(190)
        if steam is not None:
            self._play_button.setToolTip(
                f"Close ModSync and carry on with the Steam launch, which starts {steam.hands_off_to}."
            )
            self._play_button.clicked.connect(lambda: self.launchDecided.emit(launchhook.EXIT_CONTINUE))
        else:
            self._play_button.setEnabled(state.has_instance)
            self._play_button.setToolTip(
                "Launch Skyrim through the chosen MO2 instance and its selected profile, "
                "with SKSE when it is installed. Choose an MO2 instance first."
            )
            self._play_button.clicked.connect(lambda: self._launch_mo2(play=True))
        row.addWidget(self._play_button)

        wizard = self._wizard_button = QPushButton("Setup wizard")
        wizard.setToolTip("Go through the setup one step at a time.")
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
        if steam is not None:
            self._steam_note = QLabel(
                f"{steam.game.name} was launched from Steam. \"{steam.continue_label}\" goes on to "
                f"{steam.hands_off_to}. \"Cancel launch\" or closing ModSync returns to Steam "
                "without starting anything."
            )
            self._steam_note.setWordWrap(True)
            col.addWidget(self._steam_note)
            self._sync_warning = QLabel("")
            self._sync_warning.setWordWrap(True)
            role(self._sync_warning, "warning")
            self._sync_warning.setVisible(False)
            col.addWidget(self._sync_warning)
        return col

    def _on_sync_progress(self, state: str, pct: int) -> None:
        """Steam launch: say so up top when the mod list may still be arriving."""
        if self.steam_launch is None:
            return
        arriving = state == "syncing" or pct < 100
        self._sync_warning.setVisible(arriving)
        if arriving:
            self._sync_warning.setText(
                f"⚠ Still syncing ({pct}% here). Mods may still be arriving, and continuing now "
                "uses whatever has arrived so far."
            )

    def focus_default(self) -> None:
        """Steam launch: put keyboard focus on Continue, so Enter goes straight on."""
        if self.steam_launch is not None:
            self._play_button.setFocus()

    def _reset(self) -> None:
        if self.busy:
            return
        answer = QMessageBox.question(
            self,
            "Reset setup",
            "Start over on this machine?\n\n"
            "This forgets the chosen instance, stops any syncing and clears "
            "ModSync's setup so you can set it up differently.\n\n"
            "Your mods, downloads and profiles are not deleted. Every file stays "
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
        self._usvfs = None
        if state.has_instance:
            path = QLabel(state.instance_path)
            path.setWordWrap(True)
            v.addWidget(path)
            self._setup_label = QLabel("")
            self._setup_label.setWordWrap(True)
            self._setup_label.setVisible(False)
            v.addWidget(self._setup_label)
            run_async(describe_setup, state.instance_path, on_done=self._on_setup_described,
                      on_failed=lambda _: None)
            if self.steam_launch is None:
                launch_row = QHBoxLayout()
                self._open_mo2_button = QPushButton("Open MO2")
                role(self._open_mo2_button, "primary")
                self._open_mo2_button.setToolTip("Open the chosen Mod Organizer 2 instance")
                self._open_mo2_button.clicked.connect(lambda: self._launch_mo2(play=False))
                launch_row.addWidget(self._open_mo2_button)
                launch_note = QLabel("Play Skyrim uses the profile selected in MO2, with SKSE when it is installed.")
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
            # USVFS 0.5.7+ needs a current VC++ runtime in the game prefix. Only
            # shown when it's missing; installing it is always the user's click.
            self._runtime_widget = QWidget()
            self._runtime_widget.setVisible(False)
            runtime_row = QHBoxLayout(self._runtime_widget)
            runtime_row.setContentsMargins(0, 0, 0, 0)
            self._runtime_button = QPushButton("Install Visual C++ runtime…")
            self._runtime_button.clicked.connect(self._install_prefix_runtime)
            self._runtime_status = QLabel("")
            self._runtime_status.setWordWrap(True)
            role(self._runtime_status, "warning")
            runtime_row.addWidget(self._runtime_button)
            runtime_row.addWidget(self._runtime_status, stretch=1)
            v.addWidget(self._runtime_widget)
            self._refresh_prefix_runtime()
            self._usvfs = UsvfsControls(self.service)
            self._usvfs.busyChanged.connect(self._on_busy)
            self._usvfs.status.connect(self._set_status)
            self._usvfs.failed.connect(self._on_error)
            v.addWidget(self._usvfs)
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

    def _on_setup_described(self, lines: list[str]) -> None:
        self._setup_label.setText("\n".join(lines))
        self._setup_label.setVisible(bool(lines))

    # --- VC++ runtime in the game prefix ---------------------------------------
    def _refresh_prefix_runtime(self) -> None:
        run_async(self.service.prefix_runtime_problems, on_done=self._on_prefix_runtime_checked,
                  on_failed=lambda _: None)

    def _on_prefix_runtime_checked(self, problems: list[str]) -> None:
        self._runtime_widget.setVisible(bool(problems))
        self._runtime_button.setEnabled(True)
        if problems:
            self._runtime_status.setText(
                "This MO2's virtual file system needs a newer Visual C++ runtime in the game's "
                "Proton prefix, or nothing started from MO2 will run (" + "; ".join(problems) + ")."
            )

    def _install_prefix_runtime(self) -> None:
        answer = QMessageBox.question(
            self,
            "Install Visual C++ runtime",
            "ModSync will download Microsoft's Visual C++ 2015-2022 runtime (x64), check it, and "
            "install it silently into Skyrim's Proton prefix, the game's private copy of Windows. "
            "The DLLs it replaces are backed up first. Close MO2 and the game before continuing.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._runtime_button.setEnabled(False)
        self._set_status("Installing the Visual C++ runtime into the game's prefix…")
        run_async(self.service.install_prefix_runtime,
                  on_done=lambda msg: (self._set_status(msg), self._refresh_prefix_runtime()),
                  on_failed=lambda msg: (self._on_error(msg), self._refresh_prefix_runtime()))

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
        if self.steam_launch is not None:
            # Neither start the game nor walk away while game files are rewritten.
            self._play_button.setEnabled(not self.busy)
            self._cancel_launch_button.setEnabled(not self.busy)
        else:
            self._play_button.setEnabled(ready and not self.service.launcher.running(play=True))
        self._wizard_button.setEnabled(not self.busy)
        if self._reset_button is not None:
            self._reset_button.setEnabled(not self.busy)
        self.mo2.setEnabled(not self.busy)
        self.sync.setEnabled(not self.busy)
        if self._open_mo2_button is not None:
            self._open_mo2_button.setEnabled(ready and not self.service.launcher.running(play=False))
        # Do not rewrite game files while a launch from this app is alive.
        self.game.setEnabled(not self._launching and not self._usvfs_busy and not self.service.launcher.running())

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
            f"Install a user service that starts at login and {what}, "
            "even when ModSync is closed."
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
            "Make Steam's Play button open ModSync first, so you can check the mod setup, "
            "sync state and game version before the game starts. Turning it off puts "
            "Steam's previous launcher back."
        )
        self._hook_button.clicked.connect(self._toggle_hook)
        self._hook_status = QLabel("Steam launch: checking…")
        self._hook_status.setWordWrap(True)
        hook_row.addWidget(self._hook_button)
        hook_row.addWidget(self._hook_status, stretch=1)
        v.addLayout(hook_row)
        self._hook: launchhook.LaunchHookStatus | None = None
        self._refresh_hook_status()

        # Firewall: a desktop ufw/firewalld drops pairing and sync traffic until
        # ModSync's ports are allowed. The row only appears when one is running
        # (never on a Deck), and the button toggles the rules through pkexec so
        # they can be closed again after a sync or before uninstalling.
        self._fw_widget = QWidget()
        self._fw_widget.setVisible(False)
        fw_row = QHBoxLayout(self._fw_widget)
        fw_row.setContentsMargins(0, 0, 0, 0)
        self._fw_button = QPushButton("Allow in firewall…")
        self._fw_button.clicked.connect(self._toggle_firewall)
        self._fw_status = QLabel("")
        self._fw_status.setWordWrap(True)
        fw_row.addWidget(self._fw_button)
        fw_row.addWidget(self._fw_status, stretch=1)
        v.addWidget(self._fw_widget)
        self._firewall: firewall.Check | None = None
        self._refresh_firewall()
        return box

    # --- firewall ------------------------------------------------------------
    def _refresh_firewall(self) -> None:
        run_async(
            firewall.check,
            self.service.state.firewall_rules_stamp,
            on_done=self._on_firewall_checked,
            on_failed=lambda _: None,
        )

    def _on_firewall_checked(self, chk: firewall.Check) -> None:
        self._firewall = chk
        self.sync.firewall_checked(chk)
        fw = chk.firewall
        self._fw_widget.setVisible(fw is not None)
        self._fw_button.setEnabled(True)
        if fw is None:
            return
        ports = ", ".join(f"{p}/{proto}" for p, proto, _ in firewall.PORTS)
        if chk.allowed:
            self._fw_button.setText("Remove firewall rules…")
            self._fw_button.setToolTip(
                "Delete the rules ModSync added. Asks for your password.\n"
                + firewall.manual_instructions(fw, remove=True)
            )
            role(self._fw_status, "secondary")
            self._fw_status.setText(f"Firewall: ModSync's ports are allowed in {fw.kind}.")
        else:
            self._fw_button.setText("Allow in firewall…")
            self._fw_button.setToolTip(
                "Add the rules. Asks for your password.\n"
                + firewall.manual_instructions(fw)
            )
            role(self._fw_status, "warning")
            self._fw_status.setText(
                f"Firewall: {fw.kind} is on and blocks pairing and syncing until "
                f"ModSync's ports ({ports}) are allowed."
            )

    def _toggle_firewall(self) -> None:
        chk = self._firewall
        if chk is None or chk.firewall is None:
            return
        self._fw_button.setEnabled(False)
        if chk.allowed:
            self._set_status(f"Removing ModSync's rules from {chk.firewall.kind}…")
            run_async(
                firewall.revoke,
                chk.firewall,
                on_done=lambda _: self._after_firewall("", "Firewall rules removed."),
                on_failed=lambda m: self._on_firewall_failed(m, remove=True),
            )
        else:
            self._set_status(f"Adding ModSync's rules to {chk.firewall.kind}…")
            run_async(
                firewall.allow,
                chk.firewall,
                on_done=lambda stamp: self._after_firewall(str(stamp or ""), "Firewall: ModSync's ports are now allowed."),
                on_failed=self._on_firewall_failed,
            )

    def _after_firewall(self, stamp: str, message: str) -> None:
        self.service.state.firewall_rules_stamp = stamp
        self.service.state.save()
        self._set_status(message)
        self._refresh_firewall()  # re-read the rules rather than assume

    def _on_firewall_failed(self, message: str, *, remove: bool = False) -> None:
        self._fw_button.setEnabled(True)
        if "cancelled" in message:
            self._set_status("Firewall unchanged.")
            return
        fw = (self._firewall.firewall if self._firewall else None) or firewall.Firewall("ufw")
        QMessageBox.warning(
            self,
            "Firewall",
            f"Could not change the firewall: {message}\n\nIn a terminal, run:\n\n"
            + firewall.manual_instructions(fw, remove=remove),
        )

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
            head = "switching at the next reboot" if steamos.is_steam_frame() else "switching when Steam closes"
        elif st.installed or st.selected:
            head = "partly set up"
        else:
            head = "off"
        summary = st.summary()
        for prefix in ("On. ", "Off. "):  # the head line already says which
            if summary.startswith(prefix):
                summary = summary[len(prefix):]
        self._hook_status.setText(f"Steam launch: {head}\n{summary}")
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
                "⚠ Close Steam first, because it rewrites its shortcuts on exit. "
                "Then click \"Add ModSync shortcut to Steam\" again."
            )
            return
        run_async(shortcuts.add_modsync_to_steam, on_done=self._on_steam_added, on_failed=self._on_error)

    def _on_steam_added(self, paths: list) -> None:
        if paths:
            self._set_status(
                f"Added a ModSync shortcut for {len(paths)} Steam user(s). Start Steam to find it "
                "in your library and in Gaming Mode."
            )
        else:
            self._set_status(
                "No Steam users found. Is Steam installed, and has it been run at least once?"
            )

    # --- diagnostics -----------------------------------------------------------
    def _copy_diagnostics(self) -> None:
        self._diag_button.setEnabled(False)
        self._set_status("Collecting diagnostics…")
        run_async(diagnostics.build, on_done=self._on_diagnostics, on_failed=self._on_diagnostics_failed)

    def _on_diagnostics(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        self._diag_button.setEnabled(True)
        self._set_status("Diagnostics copied to the clipboard. Paste them into your bug report.")

    def _on_diagnostics_failed(self, message: str) -> None:
        self._diag_button.setEnabled(True)
        self._on_error(f"Could not collect diagnostics: {message}")

    # --- plumbing ------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.game.busy or self._launching or self._usvfs_busy

    @property
    def _usvfs_busy(self) -> bool:
        return self._usvfs is not None and self._usvfs.busy

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
