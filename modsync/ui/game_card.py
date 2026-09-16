"""The **Game** card: which Skyrim runtime is installed, which one this setup was
built for, and the buttons that fix a mismatch (downgrade with community patches,
pin the Steam manifest so it keeps launching, or re-record the version).

Needs nothing but Steam: it works before an MO2 instance is chosen and never
touches Syncthing. The dashboard and the wizard's game-version step both embed it.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modsync.downgrade.engine import Progress
from modsync.games import SKYRIM_SE
from modsync.service import GameStatus, ModSyncService, PinOutcome
from modsync.ui.worker import run_async


class _ProgressBridge(QObject):
    """Marshals engine progress callbacks from the worker thread to the UI."""

    progressed = Signal(object)


class GameCard(QGroupBox):
    status = Signal(str)  # one-line messages for the host's status line
    changed = Signal()  # the game files or the setup record were modified
    busyChanged = Signal(bool)  # a downgrade is rewriting game files

    def __init__(self, service: ModSyncService, parent: QWidget | None = None) -> None:
        super().__init__(f"{SKYRIM_SE.name} version", parent)
        self.service = service
        self._game: GameStatus | None = None
        self._downgrading = False

        layout = QVBoxLayout(self)
        self._label = QLabel("checking…")
        self._label.setWordWrap(True)
        layout.addWidget(self._label)

        row = QHBoxLayout()
        self._adopt = QPushButton("Use this machine's version")
        self._adopt.setToolTip(
            "Record the runtime installed here as the version this setup is built for. "
            "Do this after you upgrade or downgrade the game on purpose."
        )
        self._adopt.clicked.connect(self._adopt_version)
        self._adopt.setVisible(False)
        row.addWidget(self._adopt)
        self._downgrade = QPushButton("Downgrade…")
        self._downgrade.setToolTip(
            "Rewrite the game files to the version this setup needs using community "
            "xdelta patches (Mulderland). Steam keeps launching the game normally."
        )
        self._downgrade.clicked.connect(self._start_downgrade)
        self._downgrade.setVisible(False)
        row.addWidget(self._downgrade)
        self._pin = QPushButton("Keep this version")
        self._pin.setToolTip(
            "Steam wants to update the game. Pin the installed files so Steam treats "
            "them as current and launches without updating. Needs Steam closed; "
            "otherwise it is queued and applied when Steam restarts."
        )
        self._pin.clicked.connect(self._pin_version)
        self._pin.setVisible(False)
        row.addWidget(self._pin)
        row.addStretch(1)
        self._refresh_btn = QPushButton("Check again")
        self._refresh_btn.setToolTip("Re-read the installed version, SKSE and Steam's update state")
        self._refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self._refresh_btn)
        layout.addLayout(row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)
        layout.addWidget(self._progress)
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: palette(mid);")
        self._progress_label.setVisible(False)
        layout.addWidget(self._progress_label)

        self.refresh()

    # --- data flow ----------------------------------------------------------
    @property
    def game(self) -> GameStatus | None:
        return self._game

    def refresh(self) -> None:
        self._refresh_btn.setEnabled(False)
        run_async(
            self.service.game_status,
            on_done=self._on_game_status,
            on_failed=self._on_check_failed,
        )

    def poll(self) -> None:
        """Called by the host's timer: apply a queued Steam pin once Steam exits."""
        if self._game is None or not self._game.pending_pin:
            return
        run_async(self.service.apply_pending_pin, on_done=self._on_pending_pin_applied, on_failed=lambda _: None)

    def _on_check_failed(self, message: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._label.setText(f"⚠ Version check failed: {message}")

    def _on_game_status(self, st: GameStatus) -> None:
        self._refresh_btn.setEnabled(True)
        self._game = st
        vc = self.service.game_version_check()  # local and cheap; reuses the wording
        has_instance = self.service.state.has_instance
        lines = []
        if vc.installed is None and st.game_dir is None:
            lines.append(
                f"•  {SKYRIM_SE.name} was not found through Steam on this machine. Install "
                "it in Steam first."
            )
            self._label.setStyleSheet("color: palette(mid);")
        elif not has_instance and vc.installed is not None:
            # No setup to compare against yet: just report what is here.
            lines.append(f"•  {SKYRIM_SE.name} runtime here: {vc.installed}.")
            self._label.setStyleSheet("color: palette(mid);")
        elif vc.mismatch or vc.skse_suggests:
            lines.append(f"⚠  {vc.summary()}")
            self._label.setStyleSheet("color: palette(text);")
        elif vc.ok:
            lines.append(f"✅  {vc.summary()}")
            self._label.setStyleSheet("color: palette(mid);")
        else:
            lines.append(f"•  {vc.summary()}")
            self._label.setStyleSheet("color: palette(mid);")
        note = vc.skse_note()
        if note:
            lines.append(f"{'⚠' if vc.skse_suggests else '•'}  {note}")
        if st.needs_pin:
            lines.append(
                "⚠  Steam wants to update the game on its next launch. “Keep this version” "
                "makes Steam treat the installed files as current."
            )
        if st.pending_pin:
            lines.append("•  A pin is queued; it applies automatically the next time Steam is closed.")
        if st.needs_downgrade and st.suggested_target is None and st.installed is not None and st.recipe_from:
            if str(st.installed) != st.recipe_from:
                lines.append(
                    f"•  Downgrade recipes currently start from {st.recipe_from}; let Steam "
                    f"update the game first, then downgrade to {st.wanted}."
                )
            else:
                lines.append(f"•  No recipe reaches {st.wanted} yet (targets: {', '.join(st.recipe_targets)}).")
        self._label.setText("\n".join(lines))
        # Offer to (re)record only when there is a setup to record into and it
        # would change what the record says.
        self._adopt.setVisible(
            has_instance and vc.installed is not None and not vc.ok and not self._downgrading
        )
        target = st.suggested_target
        self._downgrade.setVisible(target is not None and not self._downgrading)
        if target:
            self._downgrade.setText(f"Downgrade to {target}…")
        self._pin.setVisible(st.needs_pin and not st.pending_pin and not self._downgrading)

    # --- downgrade ----------------------------------------------------------
    def _start_downgrade(self) -> None:
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
        why = (
            f"{target} is what the installed SKSE ({st.skse_source}) is built for.\n\n"
            if st.wanted_from == "skse"
            else ""
        )
        answer = QMessageBox.question(
            self,
            f"Downgrade {SKYRIM_SE.name} to {target}",
            f"{why}"
            f"This rewrites the {SKYRIM_SE.name} files in Steam's folder from {st.installed} to {target} "
            "using xdelta patches published by Mulderland (open source, checksummed).\n\n"
            "• Roughly 1 GB is downloaded and kept for next time.\n"
            "• Steam keeps launching the game normally afterwards.\n"
            "• To go back to the current version, use “Verify integrity of game files” in Steam.\n"
            f"{deck_note}\nProceed?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._downgrading = True
        self.busyChanged.emit(True)
        for btn in (self._downgrade, self._adopt, self._pin):
            btn.setVisible(False)
        self._refresh_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress_label.setText("Starting…")
        bridge = _ProgressBridge(self)
        bridge.progressed.connect(self._on_downgrade_progress)
        run_async(
            self.service.run_downgrade,
            target,
            lambda p: bridge.progressed.emit(p),
            on_done=self._on_downgraded,
            on_failed=self._on_downgrade_failed,
        )

    @property
    def busy(self) -> bool:
        """A downgrade is rewriting game files; don't navigate away."""
        return self._downgrading

    def _on_downgrade_progress(self, p: Progress) -> None:
        if p.stage == "download" and p.total:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(100 * (p.done or 0) / p.total))
            self._progress_label.setText(
                f"Downloading {p.message}: {(p.done or 0) / 1e6:,.0f} / {p.total / 1e6:,.0f} MB"
            )
        elif p.total:
            self._progress.setRange(0, p.total)
            self._progress.setValue(p.done or 0)
            self._progress_label.setText(f"{p.stage.capitalize()}: {p.message}")
        else:
            self._progress.setRange(0, 0)
            self._progress_label.setText(f"{p.stage.capitalize()}: {p.message}")

    def _end_downgrade(self) -> None:
        self._downgrading = False
        self.busyChanged.emit(False)
        self._progress.setVisible(False)
        self._progress_label.setVisible(False)
        self.refresh()

    def _on_downgraded(self, result: object) -> None:
        self._end_downgrade()
        version = getattr(result, "installed_version", "?")
        notes = " ".join(getattr(result, "notes", []) or [])
        self.status.emit(f"Downgrade complete — the game now reports {version}. {notes}".strip())
        self.changed.emit()

    def _on_downgrade_failed(self, message: str) -> None:
        self._end_downgrade()
        self.status.emit(f"⚠ Downgrade failed: {message}")

    # --- pin / record -------------------------------------------------------
    def _pin_version(self) -> None:
        run_async(self.service.pin_game_version, on_done=self._on_pinned, on_failed=self._on_error)

    def _on_pinned(self, out: PinOutcome) -> None:
        self.status.emit(out.message)
        self.refresh()

    def _on_pending_pin_applied(self, out: object) -> None:
        if out is not None:
            self.status.emit(getattr(out, "message", "Pin applied."))
            self.refresh()

    def _adopt_version(self) -> None:
        run_async(
            self.service.adopt_local_game_version,
            on_done=self._on_version_adopted,
            on_failed=self._on_error,
        )

    def _on_version_adopted(self, meta: object) -> None:
        if meta is None:
            self._on_error("Could not detect the game version on this machine.")
            return
        self.status.emit(
            "Recorded this machine's game version as the one this setup is built for. "
            "Machines that share the setup will be warned if theirs differs."
        )
        self.refresh()
        self.changed.emit()

    def _on_error(self, message: str) -> None:
        self.status.emit(f"⚠ {message}")
