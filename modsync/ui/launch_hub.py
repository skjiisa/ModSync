"""The **launch hub**: what Steam's Play button opens when the launch hook is on.

One maximized window (Gaming Mode renders anything smaller badly) that answers
"is this setup ready to play?" before the game or Mod Organizer starts:

* the mod setup in use — instance, selected profile, enabled mod count;
* the game version card (same one as the dashboard: installed vs. expected
  runtime, SKSE runtime, Steam update state, with Downgrade / Keep buttons);
* sync — off, or the live vault state so a half-arrived mod list is noticed.

**Continue** exits 0 and the hook carries the same Steam launch on;
**Cancel** (or closing the window) exits 10 and the hook ends the launch
cleanly. Continue is the default button so gamepad A / Enter goes straight on.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from modsync import __version__, launchhook
from modsync.games import GAMES, SKYRIM_SE
from modsync.mo2 import instance as mo2_instance
from modsync.service import ModSyncService, SyncStatus
from modsync.ui.game_card import GameCard
from modsync.ui.theme import role
from modsync.ui.worker import run_async

_POLL_MS = 4000
# Testing aid: MODSYNC_HUB_AUTO_DECISION=continue|cancel decides by itself after a
# few seconds, so the whole Steam → hook → hub → game chain can be exercised
# without a hand on the controller (e.g. from the game's launch options).
_AUTO_DECISION_ENV = "MODSYNC_HUB_AUTO_DECISION"
_AUTO_DECISION_MS = 3000


def describe_setup(instance_path: str | None) -> list[str]:
    """Lines about the MO2 setup in use — cheap, file-system only."""
    if not instance_path or not Path(instance_path).is_dir():
        return ["No Mod Organizer 2 instance is chosen in ModSync."]
    info = mo2_instance.inspect(instance_path)
    lines = [str(info.path)]
    if not info.has_ini:
        lines.append("Mod Organizer 2 has not been started yet (no ModOrganizer.ini).")
        return lines
    profile = info.selected_profile or "(none)"
    enabled: int | None = None
    profiles_dir = info.content_dirs["profiles"].path if "profiles" in info.content_dirs else None
    if profiles_dir and info.selected_profile:
        modlist = profiles_dir / info.selected_profile / "modlist.txt"
        try:
            enabled = sum(1 for line in modlist.read_text(encoding="utf-8", errors="replace").splitlines() if line.startswith("+"))
        except OSError:
            enabled = None
    detail = f"Profile: {profile}"
    if enabled is not None:
        detail += f"  ·  {enabled} mods enabled"
    if info.game_name:
        detail += f"  ·  {info.game_name}"
    lines.append(detail)
    for issue in info.issues:
        lines.append(f"⚠  {issue}")
    return lines


class LaunchHub(QMainWindow):
    def __init__(self, service: ModSyncService, *, appid: int | None = None, through: str | None = None) -> None:
        super().__init__()
        self.service = service
        self.game = GAMES.get(appid or SKYRIM_SE.appid, SKYRIM_SE)
        self.decision: int | None = None
        self.setWindowTitle(f"ModSync {__version__}: {self.game.name}")
        self.resize(1120, 780)

        outer = QWidget()
        self.setCentralWidget(outer)
        layout = QVBoxLayout(outer)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(14)

        state = service.state
        title = QLabel(state.instance_label if state.has_instance else "ModSync")
        role(title, "title")
        title.setWordWrap(True)
        layout.addWidget(title)
        self.hands_off_to = launchhook.describe_target(through)
        subtitle = QLabel(
            f"{self.game.name} was launched from Steam. Continue goes on to {self.hands_off_to}. "
            "Cancel returns to Steam without starting anything."
        )
        subtitle.setWordWrap(True)
        role(subtitle, "secondary")
        layout.addWidget(subtitle)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)

        setup_box = QGroupBox("Mod setup")
        setup_layout = QVBoxLayout(setup_box)
        self._setup_label = QLabel("\n".join(describe_setup(state.instance_path)))
        self._setup_label.setWordWrap(True)
        setup_layout.addWidget(self._setup_label)
        body_layout.addWidget(setup_box)

        self.game_card = GameCard(service, refresh_index=False)
        self.game_card.status.connect(self._set_status)
        self.game_card.busyChanged.connect(self._on_busy)
        body_layout.addWidget(self.game_card)

        sync_box = QGroupBox("Sync")
        sync_layout = QVBoxLayout(sync_box)
        self._sync_label = QLabel("")
        self._sync_label.setWordWrap(True)
        sync_layout.addWidget(self._sync_label)
        body_layout.addWidget(sync_box)
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

        self._status_line = QLabel("")
        role(self._status_line, "secondary")
        self._status_line.setWordWrap(True)
        layout.addWidget(self._status_line)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setToolTip("End this Steam launch without starting anything (Esc or B)")
        self.cancel_button.clicked.connect(self.cancel)
        self.continue_button = QPushButton(launchhook.continue_label(through, self.game))
        self.continue_button.setToolTip("Carry on with the same Steam launch (Enter or A)")
        role(self.continue_button, "primary")
        self.continue_button.setDefault(True)
        self.continue_button.clicked.connect(self.proceed)
        for b in (self.cancel_button, self.continue_button):
            b.setMinimumHeight(44)
            b.setMinimumWidth(200)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.continue_button)
        layout.addLayout(buttons)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.cancel)
        self.continue_button.setFocus()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.game_card.poll)
        if state.syncing:
            self._sync_label.setText("Checking the vault…")
            self._timer.timeout.connect(self._refresh_sync)
            self._refresh_sync()
        elif state.has_instance:
            self._sync_label.setText("Off. This machine's setup is not shared.")
            role(self._sync_label, "secondary")
        else:
            self._sync_label.setText("Not set up.")
            role(self._sync_label, "secondary")
        self._timer.start(_POLL_MS)

        auto = os.environ.get(_AUTO_DECISION_ENV, "").strip().lower()
        if auto in ("continue", "cancel"):
            self._set_status(f"Test mode: choosing \"{auto}\" automatically in a moment.")
            QTimer.singleShot(_AUTO_DECISION_MS, self.proceed if auto == "continue" else self.cancel)

    # --- sync ----------------------------------------------------------------
    def _refresh_sync(self) -> None:
        run_async(self.service.status, on_done=self._on_sync_status, on_failed=self._on_sync_failed)

    def _on_sync_status(self, st: SyncStatus) -> None:
        connected = sum(1 for d in st.devices if d.connected)
        pct = int(st.completion) if st.completion is not None else None
        if st.folder_state in (None, "idle") and (pct is None or pct >= 100):
            text = f"In sync. {connected} of {len(st.devices)} paired machine(s) connected."
            role(self._sync_label, "secondary")
        elif st.folder_state == "syncing" or (pct is not None and pct < 100):
            text = (
                f"⚠  Still syncing ({pct if pct is not None else '?'}% here). Mods may still be arriving. "
                "Playing now uses whatever has arrived so far."
            )
            role(self._sync_label, "warning")
        else:
            text = f"Vault {st.folder_state or 'unknown'}. {connected} of {len(st.devices)} machine(s) connected."
            role(self._sync_label, "secondary")
        self._sync_label.setText(text)

    def _on_sync_failed(self, message: str) -> None:
        self._sync_label.setText(f"Sync state unavailable right now ({message}).")
        role(self._sync_label, "secondary")

    # --- decisions -----------------------------------------------------------
    def _set_status(self, message: str) -> None:
        self._status_line.setText(message)

    def _on_busy(self, busy: bool) -> None:
        # A downgrade or restore is rewriting the game files: neither start the game nor
        # walk away from the rewrite mid-way.
        self.continue_button.setEnabled(not busy)
        self.cancel_button.setEnabled(not busy)

    def proceed(self) -> None:
        self._finish(launchhook.EXIT_CONTINUE)

    def cancel(self) -> None:
        self._finish(launchhook.EXIT_CANCEL)

    def _finish(self, code: int) -> None:
        if self.decision is not None or self.game_card.busy:
            return
        self.decision = code
        self._timer.stop()
        app = QApplication.instance()
        if app is not None:
            app.exit(code)
        self.close()

    def closeEvent(self, event) -> None:  # noqa: ANN001 (Qt signature)
        if self.game_card.busy:
            event.ignore()
            return
        if self.decision is None:
            self.decision = launchhook.EXIT_CANCEL  # closing the window never starts the game
            app = QApplication.instance()
            if app is not None:
                app.exit(self.decision)
        self._timer.stop()
        super().closeEvent(event)
