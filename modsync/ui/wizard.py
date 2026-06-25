"""In-window onboarding wizard (custom, not QWizard — single window, no modal
dialogs, per Steam Deck Gaming-Mode constraints).

Collects: the MO2 instance to sync (an existing one, or a fresh one installed via
a guided MO2-LINT install), and whether to create a new vault or join an existing
one. Emits ``completed(dict)`` for the main window to act on."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from modsync import platforms
from modsync.games import SKYRIM_SE, Game
from modsync.mo2 import discover as mo2_discover
from modsync.mo2.installers import InstallerBackend, InstallResult, Mo2LintBackend
from modsync.pairing_code import PairingCode
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


class WelcomePage(Page):
    title = "Welcome to ModSync"
    subtitle = "Sync your Mod Organizer 2 setup across machines."

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        intro = QLabel(
            "ModSync keeps your mods, load order, and downloads in sync between "
            "machines using Syncthing — while each machine keeps its own local game "
            "paths (so Steam Deck and a desktop can share one setup).\n\n"
            "This wizard sets up syncing on THIS machine."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addStretch(1)


class ChooseInstancePage(Page):
    title = "Choose your Mod Organizer 2 instance"
    subtitle = "Pick an existing MO2 instance, browse to one, or install a fresh one here."

    def __init__(self, installer: InstallerBackend, game: Game) -> None:
        super().__init__()
        self._installer = installer
        self._game = game
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
        self._chosen.setStyleSheet("color: palette(highlight);")
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
        self._run_btn.clicked.connect(self._start_install)
        dest_row.addWidget(self._dest_edit, stretch=1)
        dest_row.addWidget(choose)
        dest_row.addWidget(self._run_btn)
        v.addLayout(dest_row)

        note = QLabel(
            "Close Steam before installing. This downloads Mod Organizer 2 and "
            "configures the game's Proton prefix (can take several minutes). "
            "SKSE is not installed automatically yet."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
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


class VaultPage(Page):
    title = "Create or join a vault"
    subtitle = "A vault links this instance with your other machines."

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)

        self.create_radio = QRadioButton(
            "Create a new vault  (this machine has my current mod setup)"
        )
        self.join_radio = QRadioButton(
            "Join an existing vault  (I already set one up on another machine)"
        )
        self.create_radio.setChecked(True)
        group = QButtonGroup(self)
        group.addButton(self.create_radio)
        group.addButton(self.join_radio)

        layout.addWidget(self.create_radio)
        layout.addWidget(self.join_radio)

        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText(
            "Paste the pairing code from the other machine (MODSYNC1-…)"
        )
        self.code_edit.setEnabled(False)
        layout.addWidget(self.code_edit)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._hint)
        layout.addStretch(1)

        self.join_radio.toggled.connect(self.code_edit.setEnabled)
        self.join_radio.toggled.connect(lambda *_: self.completenessChanged.emit())
        self.code_edit.textChanged.connect(lambda *_: self.completenessChanged.emit())

    @property
    def mode(self) -> str:
        return "join" if self.join_radio.isChecked() else "create"

    @property
    def pairing_code(self) -> str:
        return self.code_edit.text().strip()

    def is_complete(self) -> bool:
        if self.mode == "create":
            self._hint.setText("You'll get a pairing code to share with your other machines.")
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

    def __init__(
        self,
        installer: InstallerBackend | None = None,
        game: Game = SKYRIM_SE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._choose = ChooseInstancePage(installer or Mo2LintBackend(), game)
        self._vault = VaultPage()
        self._pages: list[Page] = [WelcomePage(), self._choose, self._vault]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(8)

        self._title = QLabel()
        title_font = self._title.font()
        title_font.setPointSize(20)
        title_font.setBold(True)
        self._title.setFont(title_font)
        self._subtitle = QLabel()
        self._subtitle.setStyleSheet("color: palette(mid);")
        self._subtitle.setWordWrap(True)
        outer.addWidget(self._title)
        outer.addWidget(self._subtitle)
        outer.addSpacing(8)

        self._stack = QStackedWidget()
        for page in self._pages:
            page.completenessChanged.connect(self._update_nav)
            page.busyChanged.connect(self._set_busy)
            self._stack.addWidget(page)
        outer.addWidget(self._stack, stretch=1)

        footer = QHBoxLayout()
        self._back = QPushButton("Back")
        self._back.clicked.connect(self._on_back)
        self._next = QPushButton("Next")
        self._next.clicked.connect(self._on_next)
        footer.addWidget(self._back)
        footer.addStretch(1)
        footer.addWidget(self._next)
        outer.addLayout(footer)

        self._index = 0
        self._busy = False
        self._go_to(0)

    def _go_to(self, index: int) -> None:
        self._index = max(0, min(index, len(self._pages) - 1))
        page = self._pages[self._index]
        self._stack.setCurrentIndex(self._index)
        self._title.setText(page.title)
        self._subtitle.setText(page.subtitle)
        page.on_show()
        self._update_nav()

    def _update_nav(self) -> None:
        page = self._pages[self._index]
        is_last = self._index == len(self._pages) - 1
        self._back.setEnabled(self._index > 0 and not self._busy)
        self._next.setText("Finish" if is_last else "Next")
        self._next.setEnabled(page.is_complete() and not self._busy)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_nav()

    def _on_back(self) -> None:
        self._go_to(self._index - 1)

    def _on_next(self) -> None:
        if self._index < len(self._pages) - 1:
            self._go_to(self._index + 1)
        else:
            self.completed.emit(
                {
                    "instance_path": self._choose.instance_path,
                    "mode": self._vault.mode,
                    "pairing_code": self._vault.pairing_code,
                }
            )
