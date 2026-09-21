"""In-window onboarding wizard (custom, not QWizard — single window, no modal
dialogs, per Steam Deck Gaming-Mode constraints).

Steps: choose the MO2 instance (an existing one, or a fresh one installed via a
guided MO2-LINT install) → check the game version and fix it if needed → decide
whether to sync with another machine, which is optional. The instance is
remembered as soon as it is chosen, so the game step can compare against it and
finishing with "Not now" leaves nothing else to do. Emits ``completed(dict)`` for
the main window to act on."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from modsync import pairing_lan, platforms
from modsync.games import SKYRIM_SE, Game
from modsync.mo2 import discover as mo2_discover
from modsync.mo2.installers import InstallerBackend, InstallResult, Mo2LintBackend
from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.ui.game_card import GameCard
from modsync.ui.theme import role
from modsync.ui.worker import run_async


class _LineEmitter(QObject):
    """Marshals installer output lines from a worker thread to the UI thread."""

    line = Signal(str)


class Page(QWidget):
    completenessChanged = Signal()
    busyChanged = Signal(bool)
    title = ""
    subtitle = ""

    def is_complete(self) -> bool:
        return True

    def on_show(self) -> None:
        pass

    def commit(self, on_done, on_failed) -> bool:
        """Called when leaving the page forwards. Return True to advance now, or
        False to wait: call ``on_done()`` to advance later or ``on_failed(msg)``
        to stay."""
        return True


class WelcomePage(Page):
    title = "Welcome to ModSync"
    subtitle = f"Set up {SKYRIM_SE.name} for modding on this machine."

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        for title, description in (
            ("1 · Mod Organizer 2", "Choose an existing instance or install a fresh one for Skyrim."),
            ("2 · Sync (optional)", "Share mods and load order with another machine, or keep this setup local."),
            (
                "3 · Game version",
                "Check Skyrim and SKSE, and keep the version your mods need. If you are copying "
                "mods from another machine, this happens on the dashboard once they have arrived.",
            ),
        ):
            box = QGroupBox(title)
            content = QVBoxLayout(box)
            label = QLabel(description)
            label.setWordWrap(True)
            content.addWidget(label)
            layout.addWidget(box)
        layout.addStretch(1)


class ChooseInstancePage(Page):
    title = "Choose your Mod Organizer 2 instance"
    subtitle = "Pick an existing MO2 instance, browse to one, or install a fresh one here."

    def __init__(
        self, installer: InstallerBackend, game: Game, service: ModSyncService | None = None
    ) -> None:
        super().__init__()
        self._installer = installer
        self._game = game
        self._service = service
        self._path: str | None = None
        self._installing = False
        self._buttons = QButtonGroup(self)
        self._scanned = False

        self._emitter = _LineEmitter()

        layout = QVBoxLayout(self)
        self._status = QLabel("Scanning for MO2 instances…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._radios = QVBoxLayout()
        layout.addLayout(self._radios)

        row = QHBoxLayout()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self._install_btn = QPushButton(f"Set up MO2 for {game.name} here…")
        self._install_btn.clicked.connect(self._toggle_install_panel)
        row.addWidget(browse)
        row.addWidget(self._install_btn)
        row.addStretch(1)
        layout.addLayout(row)

        # Guided-install panel (hidden until requested).
        self._panel = self._build_install_panel()
        self._panel.setVisible(False)
        layout.addWidget(self._panel)

        self._chosen = QLabel("")
        self._chosen.setWordWrap(True)
        role(self._chosen, "secondary")
        layout.addWidget(self._chosen)
        layout.addStretch(1)

        self._emitter.line.connect(self._append_log)

    def _build_install_panel(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 8, 0, 0)

        dest_row = QHBoxLayout()
        dest_row.addWidget(QLabel("Install to:"))
        self._dest_edit = QLineEdit(str(Path.home() / "ModOrganizer2-SkyrimSE"))
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_dest)
        self._run_btn = QPushButton("Install")
        role(self._run_btn, "primary")
        self._run_btn.clicked.connect(self._start_install)
        dest_row.addWidget(self._dest_edit, stretch=1)
        dest_row.addWidget(choose)
        dest_row.addWidget(self._run_btn)
        v.addLayout(dest_row)

        note = QLabel(
            "Close Steam before installing. This downloads Mod Organizer 2 and "
            "sets up the game's Proton prefix, which can take several minutes. "
            "SKSE can be installed afterwards from the Game version step."
        )
        note.setWordWrap(True)
        role(note, "secondary")
        v.addWidget(note)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setVisible(False)
        self._log.setMaximumBlockCount(2000)
        v.addWidget(self._log)
        return panel

    # --- discovery ---
    def on_show(self) -> None:
        if self._scanned:
            return
        self._scanned = True
        run_async(
            self._scan,
            on_done=self._populate,
            on_failed=lambda e: self._status.setText(f"Scan failed: {e}"),
        )

    @staticmethod
    def _scan() -> list[str]:
        plat = platforms.current()
        found = mo2_discover.discover_instances(
            plat.mo2_broad_roots(), plat.mo2_known_roots()
        )
        return [str(p) for p in found]

    def _populate(self, paths: list[str]) -> None:
        if not paths:
            self._status.setText(
                "No MO2 instances found automatically. Browse to one, or set one up here."
            )
            return
        self._status.setText("Found these MO2 instances:")
        for path in paths:
            radio = QRadioButton(path)
            radio.toggled.connect(
                lambda checked, p=path: self._select(p) if checked else None
            )
            self._buttons.addButton(radio)
            self._radios.addWidget(radio)

    # --- existing-instance selection ---
    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select MO2 instance folder")
        if folder:
            self._buttons.setExclusive(False)
            for b in self._buttons.buttons():
                b.setChecked(False)
            self._buttons.setExclusive(True)
            self._select(folder)

    def _select(self, path: str) -> None:
        self._path = path
        self._chosen.setText(f"Selected: {path}")
        self.completenessChanged.emit()

    # --- guided install ---
    def _toggle_install_panel(self) -> None:
        self._panel.setVisible(not self._panel.isVisible())

    def _choose_dest(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose install location")
        if folder:
            self._dest_edit.setText(str(Path(folder) / f"ModOrganizer2-{self._game.mo2lint_key}"))

    def _append_log(self, line: str) -> None:
        self._log.appendPlainText(line)

    def _start_install(self) -> None:
        ok, reason = self._installer.available()
        if not ok:
            self._log.setVisible(True)
            self._log.appendPlainText(f"Cannot install: {reason}")
            return
        dest = self._dest_edit.text().strip()
        if not dest:
            return
        self._set_installing(True)
        self._log.clear()
        self._log.setVisible(True)
        self._log.appendPlainText(f"Installing MO2 for {self._game.name} to {dest} …")
        run_async(
            self._installer.install,
            self._game,
            dest,
            script_extender=False,
            on_output=self._emitter.line.emit,
            on_done=self._on_install_done,
            on_failed=self._on_install_failed,
        )

    def _on_install_done(self, result: InstallResult) -> None:
        self._set_installing(False)
        if result.success and result.instance_path:
            self._select(str(result.instance_path))
            self._log.appendPlainText(f"\n✓ Installed to {result.instance_path}")
        else:
            self._log.appendPlainText(f"\n✗ Install failed: {result.message}")

    def _on_install_failed(self, message: str) -> None:
        self._set_installing(False)
        self._log.appendPlainText(f"\n✗ {message}")

    def _set_installing(self, installing: bool) -> None:
        self._installing = installing
        self._run_btn.setEnabled(not installing)
        self._dest_edit.setEnabled(not installing)
        self.busyChanged.emit(installing)
        self.completenessChanged.emit()

    def is_complete(self) -> bool:
        return self._path is not None and not self._installing

    @property
    def instance_path(self) -> str | None:
        return self._path

    def commit(self, on_done, on_failed) -> bool:
        if self._service is None or not self._path:
            return True
        self._chosen.setText(f"Reading {self._path}…")
        run_async(
            self._service.choose_instance,
            self._path,
            on_done=lambda _: (self._chosen.setText(f"Selected: {self._path}"), on_done()),
            on_failed=lambda m: (self._chosen.setText(f"⚠ {m}"), on_failed(m)),
        )
        return False


class GameVersionPage(Page):
    title = "Game version"
    subtitle = (
        "SKSE and native DLL mods only load on the exact game version they were built "
        "for. Steam updates the game silently. This step puts it back."
    )

    def __init__(self, service: ModSyncService) -> None:
        super().__init__()
        self._note = QLabel("")
        self._note.setWordWrap(True)
        self.card = GameCard(service)
        self.card.busyChanged.connect(self.busyChanged.emit)
        self.card.busyChanged.connect(lambda *_: self.completenessChanged.emit())
        self.card.status.connect(self._note.setText)
        layout = QVBoxLayout(self)
        layout.addWidget(self.card)
        hint = QLabel(
            "If there is nothing to fix, continue. A downgrade takes a few minutes and "
            "about 1 GB of downloads, which are kept for next time."
        )
        hint.setWordWrap(True)
        role(hint, "secondary")
        layout.addWidget(hint)
        layout.addWidget(self._note)
        layout.addStretch(1)

    def on_show(self) -> None:
        self.card.refresh()  # the instance was chosen on the previous page

    def is_complete(self) -> bool:
        return not self.card.busy


class VaultPage(Page):
    title = "Sync with another machine"
    subtitle = "Optional. Keep this setup on another machine, such as a desktop and a Steam Deck."

    def __init__(self) -> None:
        super().__init__()
        self._announcements: list = []
        self._selected = None

        layout = QVBoxLayout(self)

        self.local_radio = QRadioButton("Not now (just use this machine)")
        self.create_radio = QRadioButton(
            "Share from this machine (it has my current mod setup)"
        )
        self.network_radio = QRadioButton(
            "Find a machine on my network (no code to type)"
        )
        self.join_radio = QRadioButton(
            "Join with a pairing code (paste a code from another machine)"
        )
        self.local_radio.setChecked(True)
        group = QButtonGroup(self)
        for radio in (self.local_radio, self.create_radio, self.network_radio, self.join_radio):
            group.addButton(radio)
            layout.addWidget(radio)

        # network discovery panel (hidden unless its radio is selected)
        self._net_panel = QWidget()
        nv = QVBoxLayout(self._net_panel)
        nv.setContentsMargins(24, 4, 0, 0)
        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("1. Pick the machine with the mods (it must be showing a PIN):"))
        scan_row.addStretch(1)
        self._scan_btn = QPushButton("Scan")
        self._scan_btn.clicked.connect(self._scan)
        scan_row.addWidget(self._scan_btn)
        nv.addLayout(scan_row)
        self._net_list = QListWidget()
        self._net_list.itemSelectionChanged.connect(self._on_select)
        nv.addWidget(self._net_list)
        pin_row = QHBoxLayout()
        pin_row.addWidget(QLabel("2. Enter the PIN it shows:"))
        self._pin_edit = QLineEdit()
        self._pin_edit.setMaxLength(7)
        self._pin_edit.setPlaceholderText("042 815")
        self._pin_edit.setEnabled(False)  # until a machine is picked
        self._pin_edit.textChanged.connect(lambda *_: self.completenessChanged.emit())
        pin_row.addWidget(self._pin_edit, stretch=1)
        nv.addLayout(pin_row)
        self._net_panel.setVisible(False)
        layout.addWidget(self._net_panel)

        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText(
            "Paste the pairing code from the other machine (MODSYNC1-…)"
        )
        self.code_edit.setVisible(False)
        layout.addWidget(self.code_edit)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        role(self._hint, "secondary")
        layout.addWidget(self._hint)
        layout.addStretch(1)

        self.network_radio.toggled.connect(self._net_panel.setVisible)
        self.network_radio.toggled.connect(lambda on: self._scan() if on else None)
        self.join_radio.toggled.connect(self.code_edit.setVisible)
        for radio in (self.local_radio, self.create_radio, self.network_radio, self.join_radio):
            radio.toggled.connect(lambda *_: self.completenessChanged.emit())
        self.code_edit.textChanged.connect(lambda *_: self.completenessChanged.emit())

    # --- network discovery ---
    def _scan(self) -> None:
        if not self._scan_btn.isEnabled():
            return
        self._announcements = []
        self._scan_btn.setEnabled(False)
        self._selected = None
        self._net_list.clear()
        self._net_list.addItem("Scanning…")
        self.completenessChanged.emit()
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
                "No machines found. Start \"Pair over network\" on the other machine, then scan again."
            )
            self._net_list.addItem(pairing_lan.FIREWALL_HINT)
        else:
            for a in anns:
                self._net_list.addItem(f"{a.name}   ({a.host})")
        self.completenessChanged.emit()

    def _on_scan_failed(self, message: str) -> None:
        self._scan_btn.setEnabled(True)
        self._announcements = []
        self._net_list.clear()
        self._net_list.addItem(f"Scan failed: {message}")
        self.completenessChanged.emit()

    def _on_select(self) -> None:
        row = self._net_list.currentRow()
        self._selected = self._announcements[row] if 0 <= row < len(self._announcements) else None
        self._pin_edit.setEnabled(self._selected is not None)
        if self._selected is not None:
            self._pin_edit.setFocus()
        self.completenessChanged.emit()

    # --- exposed to the wizard ---
    @property
    def mode(self) -> str:
        if self.local_radio.isChecked():
            return "local"
        if self.network_radio.isChecked():
            return "network"
        return "join" if self.join_radio.isChecked() else "create"

    @property
    def joins(self) -> bool:
        """This machine receives the setup from another one: the game-version
        step can only be done once it has synced, so the wizard ends here."""
        return self.mode in ("network", "join")

    @property
    def pairing_code(self) -> str:
        return self.code_edit.text().strip()

    @property
    def pin(self) -> str:
        return self._pin_edit.text().replace(" ", "").strip()

    @property
    def announcement(self):
        return self._selected

    def is_complete(self) -> bool:
        if self.mode == "local":
            self._hint.setText("You can set up syncing any time from the dashboard.")
            return True
        if self.mode == "create":
            self._hint.setText("You will get a pairing code to share with your other machines.")
            return True
        if self.mode == "network":
            if self._selected is None:
                self._hint.setText("Pick the machine to pair with, then enter its PIN.")
                return False
            if len(self.pin) != 6 or not self.pin.isdigit():
                self._hint.setText("Enter the 6-digit PIN shown on the other machine.")
                return False
            self._hint.setText(f"Will pair with {self._selected.name}.")
            return True
        try:
            PairingCode.decode(self.pairing_code)
            self._hint.setText("Pairing code looks valid.")
            return True
        except Exception:
            self._hint.setText("Enter the pairing code shown on your other machine.")
            return False


class WizardWidget(QWidget):
    completed = Signal(dict)
    cancelled = Signal()  # backing out of the first page returns to the dashboard

    def __init__(
        self,
        service: ModSyncService,
        installer: InstallerBackend | None = None,
        game: Game = SKYRIM_SE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._choose = ChooseInstancePage(installer or Mo2LintBackend(), game, service)
        self._game_page = GameVersionPage(service)
        self._vault = VaultPage()
        # Sync comes before the game-version check: a machine that copies its
        # mods from another one can't know which version they need until the
        # setup (and its modsync-vault.json) has arrived, so for those modes the
        # wizard ends at the sync step and the dashboard takes over.
        self._pages: list[Page] = [WelcomePage(), self._choose, self._vault, self._game_page]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(8)

        self._steps = QLabel()
        role(self._steps, "step")
        outer.addWidget(self._steps)
        self._title = QLabel()
        role(self._title, "title")
        self._title.setWordWrap(True)
        self._subtitle = QLabel()
        role(self._subtitle, "secondary")
        self._subtitle.setWordWrap(True)
        outer.addWidget(self._title)
        outer.addWidget(self._subtitle)
        outer.addSpacing(8)

        self._stack = QStackedWidget()
        for page in self._pages:
            page.completenessChanged.connect(self._update_nav)
            page.busyChanged.connect(self._set_busy)
            self._stack.addWidget(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self._stack)
        outer.addWidget(scroll, stretch=1)

        footer = QHBoxLayout()
        self._back = QPushButton("Back")
        self._back.clicked.connect(self._on_back)
        self._cancel = QPushButton("Back to dashboard")
        self._cancel.clicked.connect(self._on_cancel)
        self._next = QPushButton("Next")
        role(self._next, "primary")
        self._next.clicked.connect(self._on_next)
        footer.addWidget(self._back)
        footer.addWidget(self._cancel)
        footer.addStretch(1)
        footer.addWidget(self._next)
        outer.addLayout(footer)

        self._index = 0
        self._busy = False
        self._go_to(0)

    def open_install(self) -> None:
        self._go_to(1)
        self._choose._panel.setVisible(True)
        self._choose._dest_edit.setFocus()

    def _on_cancel(self) -> None:
        if not self.busy:
            self.cancelled.emit()

    @property
    def busy(self) -> bool:
        return self._busy

    def _go_to(self, index: int) -> None:
        self._index = max(0, min(index, len(self._pages) - 1))
        page = self._pages[self._index]
        self._stack.setCurrentIndex(self._index)
        self._steps.setText("SETUP OVERVIEW" if self._index == 0 else f"STEP {self._index} OF 3")
        self._title.setText(page.title)
        self._subtitle.setText(page.subtitle)
        page.on_show()
        self._update_nav()

    def _is_last(self) -> bool:
        if self._index == len(self._pages) - 1:
            return True
        return self._pages[self._index] is self._vault and self._vault.joins

    def _update_nav(self) -> None:
        page = self._pages[self._index]
        is_last = self._is_last()
        self._back.setText("Cancel" if self._index == 0 else "Back")
        self._back.setEnabled(not self._busy)
        self._cancel.setVisible(self._index > 0)
        self._cancel.setEnabled(not self._busy)
        self._next.setText("Finish" if is_last else "Next")
        self._next.setEnabled(page.is_complete() and not self._busy)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_nav()

    def _on_back(self) -> None:
        if self.busy:
            return
        if self._index == 0:  # nothing to go back to — leave the wizard
            self.cancelled.emit()
            return
        self._go_to(self._index - 1)

    def _on_next(self) -> None:
        if self.busy:
            return
        if not self._is_last():
            page = self._pages[self._index]
            self._set_busy(True)

            def advance() -> None:
                self._set_busy(False)
                self._go_to(self._index + 1)

            def stay(message: str) -> None:
                self._set_busy(False)

            if page.commit(advance, stay):
                advance()
        else:
            self.completed.emit(
                {
                    "instance_path": self._choose.instance_path,
                    "mode": self._vault.mode,
                    "pairing_code": self._vault.pairing_code,
                    "announcement": self._vault.announcement,
                    "pin": self._vault.pin,
                }
            )
