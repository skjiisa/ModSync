"""Ask for the 6-digit PIN the other machine is showing (and, when it wasn't
found by the scan, its address). One question per dialog, so the next step is
never in doubt: pick a machine → this pops up → type what's on its screen."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from modsync import pairing_lan
from modsync.pairing_lan import Announcement
from modsync.ui.theme import role


def pin_font(base: QFont, points: int = 30) -> QFont:
    f = QFont(base)
    f.setPointSize(points)
    f.setBold(True)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
    return f


def normalize_pin(text: str) -> str:
    return text.replace(" ", "").strip()


class PinDialog(QDialog):
    """``PinDialog(parent, announcement)`` for a machine picked from the scan;
    ``PinDialog(parent)`` to also ask for an address typed by hand."""

    def __init__(self, parent: QWidget | None, announcement: Announcement | None = None) -> None:
        super().__init__(parent)
        self._announcement = announcement
        name = announcement.name if announcement else "the machine with the mods"
        self.setWindowTitle(f"Pair with {name}" if announcement else "Pair by address")
        self.setModal(True)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        self._addr_edit: QLineEdit | None = None
        if announcement is None:
            v.addWidget(QLabel("Address of the machine with the mods:"))
            self._addr_edit = QLineEdit()
            self._addr_edit.setPlaceholderText("192.168.1.20")
            self._addr_edit.setToolTip(
                "Shown under “Pair over network” on that machine (host, or host:port)"
            )
            self._addr_edit.textChanged.connect(self._validate)
            v.addWidget(self._addr_edit)
            v.addSpacing(6)

        prompt = QLabel(
            f"Enter the PIN shown on <b>{name}</b> under “Pair over network”:"
        )
        prompt.setWordWrap(True)
        v.addWidget(prompt)

        self._pin_edit = QLineEdit()
        self._pin_edit.setFont(pin_font(self.font()))
        self._pin_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin_edit.setPlaceholderText("000 000")
        self._pin_edit.setMaxLength(12)  # room for a sloppy paste; regrouped to 6 digits
        self._pin_edit.textChanged.connect(self._on_pin_changed)
        v.addWidget(self._pin_edit)

        self._hint = QLabel("6 digits")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        role(self._hint, "secondary")
        v.addWidget(self._hint)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._pair_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._pair_btn.setText("Pair")
        self._pair_btn.setDefault(True)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        v.addWidget(self._buttons)

        self._validate()
        (self._addr_edit or self._pin_edit).setFocus()

    # --- state -----------------------------------------------------------------
    def _on_pin_changed(self, text: str) -> None:
        digits = "".join(ch for ch in text if ch.isdigit())[:6]
        shown = f"{digits[:3]} {digits[3:]}" if len(digits) > 3 else digits
        if shown != text:  # re-group as "042 815" without fighting the cursor
            self._pin_edit.blockSignals(True)
            self._pin_edit.setText(shown)
            self._pin_edit.setCursorPosition(len(shown))
            self._pin_edit.blockSignals(False)
        self._validate()

    def _validate(self) -> None:
        ok = len(self.pin()) == 6
        if self._addr_edit is not None:
            ok = ok and bool(self._addr_edit.text().strip())
        self._pair_btn.setEnabled(ok)

    def pin(self) -> str:
        return normalize_pin(self._pin_edit.text())

    def announcement(self) -> Announcement:
        """The machine to pair with; raises :class:`pairing_lan.PairError` for a
        malformed typed address."""
        if self._announcement is not None:
            return self._announcement
        assert self._addr_edit is not None
        return pairing_lan.Announcement.manual(self._addr_edit.text())
