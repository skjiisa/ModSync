"""Main window: hosts the ModSync service.

The **dashboard is always the home screen** — even before anything is set up, where
it shows an inline setup section. The linear wizard is opt-in from there (and can
be cancelled back out), so the user is never trapped in a one-way flow.

Single, self-maximizing window with no modal dialogs for the main flow, per Steam
Deck Gaming-Mode constraints.

The same window serves Steam's launch hook. Given a ``SteamLaunch``, Steam is
waiting on it: the dashboard's Continue / Cancel launch record the decision and
close the window, closing it any other way cancels, and the decision outlives
dashboard rebuilds and trips through the wizard."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from modsync import __version__, launchhook
from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.ui.dashboard import Dashboard
from modsync.ui.wizard import WizardWidget
from modsync.ui.worker import run_async

# Testing aid: MODSYNC_HUB_AUTO_DECISION=continue|cancel decides a Steam launch by
# itself after a few seconds, so the whole Steam → hook → ModSync → game chain
# can be exercised without a hand on the controller (e.g. from the game's
# launch options).
_AUTO_DECISION_ENV = "MODSYNC_HUB_AUTO_DECISION"
_AUTO_DECISION_MS = 3000


def _centered(text: str, button: QPushButton | None = None) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.addStretch(1)
    label = QLabel(text)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout.addWidget(label)
    if button is not None:
        layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignCenter)
    layout.addStretch(1)
    return widget


class MainWindow(QMainWindow):
    def __init__(self, *, steam_launch: launchhook.SteamLaunch | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"ModSync {__version__}")
        self.resize(1120, 780)

        self.steam_launch = steam_launch
        self.service = ModSyncService()
        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._show_dashboard()  # always — it handles the not-set-up case itself

        auto = os.environ.get(_AUTO_DECISION_ENV, "").strip().lower()
        if steam_launch is not None and auto in ("continue", "cancel"):
            code = launchhook.EXIT_CONTINUE if auto == "continue" else launchhook.EXIT_CANCEL
            current = self._stack.currentWidget()
            if isinstance(current, Dashboard):
                current._set_status(f"Test mode: choosing \"{auto}\" automatically in a moment.")
            QTimer.singleShot(_AUTO_DECISION_MS, self, lambda: self.decide_launch(code))

    @property
    def busy(self) -> bool:
        current = self._stack.currentWidget()
        return isinstance(current, (Dashboard, WizardWidget)) and current.busy

    def _set(self, widget: QWidget) -> None:
        while self._stack.count():
            old = self._stack.widget(0)
            if isinstance(old, Dashboard):
                old.shutdown()
            self._stack.removeWidget(old)
            old.deleteLater()
        self._stack.addWidget(widget)
        self._stack.setCurrentWidget(widget)

    def _show_wizard(self, *, install: bool = False) -> None:
        if self.busy:
            return
        wizard = WizardWidget(self.service)
        wizard.completed.connect(self._on_wizard_completed)
        wizard.cancelled.connect(self._show_dashboard)
        self._set(wizard)
        if install:
            wizard.open_install()

    def _on_wizard_completed(self, data: dict) -> None:
        path = data.get("instance_path")
        if not path:
            return
        mode = data.get("mode")
        if mode == "local":  # the wizard already remembered the instance
            self._show_dashboard()
            return
        code_text = data.get("pairing_code") or ""
        label = Path(path).name or "Mod Organizer 2"

        self._set(_centered("Setting up sync and starting Syncthing…"))

        def work() -> object:
            if mode == "network":
                return self.service.join_via_network(data["announcement"], data["pin"], path)
            if mode == "join":
                return self.service.join_vault(PairingCode.decode(code_text), path)
            return self.service.create_vault(path, label=label)

        run_async(work, on_done=lambda _: self._show_dashboard(), on_failed=self._on_setup_failed)

    def _on_setup_failed(self, message: str) -> None:
        back = QPushButton("Back to dashboard")
        back.clicked.connect(self._show_dashboard)
        self._set(_centered(f"Setup failed:\n{message}", back))

    def _show_dashboard(self) -> None:
        if self.busy:
            return
        dashboard = Dashboard(self.service, steam_launch=self.steam_launch)
        dashboard.wizardRequested.connect(self._show_wizard)
        dashboard.installRequested.connect(lambda: self._show_wizard(install=True))
        dashboard.stateChanged.connect(self._show_dashboard)  # rebuild after setup/reset
        dashboard.launchDecided.connect(self.decide_launch)
        self._set(dashboard)
        dashboard.focus_default()

    def decide_launch(self, code: int) -> None:
        """Steam launch: record Continue or Cancel, then close so the hook can act.
        The first decision sticks, and none is taken while files are rewritten."""
        launch = self.steam_launch
        if launch is None or launch.decision is not None or self.busy:
            return
        launch.decision = code
        self.close()

    def closeEvent(self, event) -> None:  # noqa: ANN001 (Qt signature)
        if self.busy:
            event.ignore()
            return
        if self.steam_launch is not None and self.steam_launch.decision is None:
            self.steam_launch.decision = launchhook.EXIT_CANCEL  # closing never starts the game
        # Order matters: stop polling, let in-flight worker jobs finish (so none
        # emit back into widgets being torn down), then stop the daemon.
        current = self._stack.currentWidget()
        if isinstance(current, Dashboard):
            current.shutdown()
        QThreadPool.globalInstance().waitForDone(5000)
        try:
            self.service.shutdown()
        except Exception:
            pass
        super().closeEvent(event)
