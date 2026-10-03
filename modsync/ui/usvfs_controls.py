"""The USVFS repair controls in the dashboard's Mod Organizer 2 card."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from modsync.mo2 import usvfs
from modsync.ui.theme import role
from modsync.ui.worker import run_async


class UsvfsControls(QWidget):
    busyChanged = Signal(bool)
    status = Signal(str)
    failed = Signal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        buttons = QHBoxLayout()
        self.apply_button = QPushButton("Apply ARM64 fix")
        self.apply_button.setToolTip(
            "Download the verified USVFS fix for MO2 2.5.2, about 13 MB, and back up the originals. "
            "Close MO2, Skyrim, and programs started by MO2 first."
        )
        self.restore_button = QPushButton("Restore original USVFS")
        self.restore_button.setToolTip("Put the backed-up files back. Games started through MO2 will need the fix again on ARM64.")
        self.apply_button.clicked.connect(lambda: self._change(False))
        self.restore_button.clicked.connect(lambda: self._change(True))
        buttons.addWidget(self.apply_button)
        buttons.addWidget(self.restore_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.setVisible(False)
        self.refresh()

    def refresh(self):
        if not self.busy:
            run_async(self.service.usvfs_status, on_done=self._checked, on_failed=self._check_failed)

    def _checked(self, result: usvfs.Status):
        self.setVisible(result.state != "missing" and (usvfs.is_arm64() or result.can_restore))
        self.label.setText(result.message)
        role(self.label, "secondary" if result.state in ("patched", "not-needed") else "warning")
        self.apply_button.setVisible(result.can_apply)
        self.restore_button.setVisible(result.can_restore)
        self.apply_button.setEnabled(result.can_apply and not self.busy)
        self.restore_button.setEnabled(result.can_restore and not self.busy)

    def _check_failed(self, message):
        self.setVisible(usvfs.is_arm64())
        self.label.setText(f"Could not check USVFS: {message}")
        self.apply_button.setEnabled(False)
        self.restore_button.setEnabled(False)

    def _change(self, restore):
        if self.busy:
            return
        self.busy = True
        self.apply_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.busyChanged.emit(True)
        self.label.setText("Restoring original USVFS…" if restore else "Downloading and applying the USVFS ARM64 fix…")
        operation = self.service.restore_usvfs if restore else self.service.apply_usvfs_fix
        run_async(operation, on_done=self._finished, on_failed=self._failed)

    def _finished(self, message):
        self._done()
        self.status.emit(message)

    def _failed(self, message):
        self._done()
        self.failed.emit(f"Could not change USVFS: {message}")

    def _done(self):
        self.busy = False
        self.busyChanged.emit(False)
        self.refresh()
