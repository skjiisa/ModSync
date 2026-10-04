"""The sections of the main window, one module each.

Every page scrolls as a whole and remembers where focus was, so switching
sections with the bumpers and back again picks up where you left off."""

from __future__ import annotations

import weakref

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from modsync.ui import nav
from modsync.ui.input import Action
from modsync.ui.widgets import PageHeader, scroller


class Page(QWidget):
    key = ""
    label = ""
    icon = ""

    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        self.service = host.service
        self._last_focus: weakref.ref | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(40, 18, 40, 28)
        self.content_layout.setSpacing(20)
        self.area = scroller(self.content)
        outer.addWidget(self.area)

    # --- layout helpers ---
    def header(self, eyebrow: str, title: str, subtitle: str = "") -> PageHeader:
        h = PageHeader(eyebrow, title, subtitle)
        self.content_layout.addWidget(h)
        return h

    def columns(self, left: int = 11, right: int = 9) -> tuple[QVBoxLayout, QVBoxLayout]:
        row = QHBoxLayout()
        row.setSpacing(24)
        a, b = QVBoxLayout(), QVBoxLayout()
        a.setSpacing(16)
        b.setSpacing(12)
        row.addLayout(a, left)
        row.addLayout(b, right)
        self.content_layout.addLayout(row)
        return a, b

    def finish_layout(self) -> None:
        self.content_layout.addStretch(1)

    # --- focus ---
    def remember(self, w: QWidget) -> None:
        if self.isAncestorOf(w):
            self._last_focus = weakref.ref(w)

    def preferred_focus(self) -> QWidget | None:
        return None

    def focus_default(self) -> None:
        last = self._last_focus() if self._last_focus is not None else None
        candidates = nav.focusables(self)
        target = last if last in candidates else None
        if target is None:
            preferred = self.preferred_focus()
            target = preferred if preferred in candidates else None
        if target is None and candidates:
            target = nav.reading_order(candidates, self)[0]
        if target is not None:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            nav.reveal(target)

    # --- behaviour ---
    def hints(self) -> list[tuple[list[Action], str]]:
        return []

    def handle_action(self, action: Action) -> bool:
        return False

    def poll(self) -> None:
        pass

    def shutdown(self) -> None:
        pass
