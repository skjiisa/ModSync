"""Main window: hosts the ModSync service.

The **dashboard is always the home screen** — even before anything is set up, where
it shows an inline setup section. The linear wizard is opt-in from there (and can
be cancelled back out), so the user is never trapped in a one-way flow.

Single, self-maximizing window with no modal dialogs for the main flow, per Steam
Deck Gaming-Mode constraints."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from modsync import __version__
from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.ui.dashboard import Dashboard
from modsync.ui.wizard import WizardWidget
from modsync.ui.worker import run_async


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
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"ModSync {__version__}")
        self.resize(1120, 780)

        self.service = ModSyncService()
        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._show_dashboard()  # always — it handles the not-set-up case itself

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

    def _show_wizard(self) -> None:
        if self.busy:
            return
        wizard = WizardWidget(self.service)
        wizard.completed.connect(self._on_wizard_completed)
        wizard.cancelled.connect(self._show_dashboard)
        self._set(wizard)

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
        dashboard = Dashboard(self.service)
        dashboard.wizardRequested.connect(self._show_wizard)
        dashboard.stateChanged.connect(self._show_dashboard)  # rebuild after setup/reset
        self._set(dashboard)

    def closeEvent(self, event) -> None:  # noqa: ANN001 (Qt signature)
        if self.busy:
            event.ignore()
            return
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
