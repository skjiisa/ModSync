"""Sheets that slide over the window instead of separate dialog windows.

Gaming Mode shows one window at a time, and a native file or message dialog
is awkward to drive with a d-pad. These sheets live inside the main window,
keep focus to themselves while open and close with B (Escape), and every one
of them works with a mouse too.

* ``ConfirmSheet``: a question with a few choices.
* ``DetailsSheet``: read-only text, such as technical details.
* ``KeyboardSheet``: text entry with an on-screen keyboard and a Paste key.
* ``PinSheet``: six digit wheels for a pairing PIN, plus an address when needed.
* ``FolderSheet``: a folder browser.
* ``BeaconSheet``: the big PIN shown while waiting for another machine.
* ``CodeSheet``: the pairing code and its QR code.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from modsync import pairing_lan
from modsync.pairing_lan import Announcement
from modsync.ui import nav, theme
from modsync.ui.input import Action
from modsync.ui.widgets import Tile, clear_layout, label, scroller

Choice = tuple[str, str, str, str, str | None]  # key, title, description, role, icon


class Overlay(QWidget):
    """A dimmed full-window layer with one centered sheet. ``host`` is the
    main window; it stacks overlays and gives focus back when one closes."""

    closed = Signal()

    def __init__(self, host, title: str, *, eyebrow: str = "", width: int = 760) -> None:
        super().__init__(host.shell)
        self.host = host
        self.busy = False
        self.dismissable = True
        self._done = False
        self.restore_focus: QWidget | None = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.sheet = QFrame()
        self.sheet.setObjectName("sheet")
        self.sheet.setMaximumWidth(width)
        self.sheet.setMinimumWidth(min(width, 560))
        row.addWidget(self.sheet, 100)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        self.body = QVBoxLayout(self.sheet)
        self.body.setContentsMargins(34, 30, 34, 30)
        self.body.setSpacing(14)
        if eyebrow:
            self.body.addWidget(label(eyebrow.upper(), "eyebrow", wrap=False))
        self.title = label(title, "title")
        self.body.addWidget(self.title)
        self.title.setVisible(bool(title))

    # --- lifecycle ---
    def open(self) -> "Overlay":
        self.host.open_overlay(self)
        return self

    def dismiss(self) -> None:
        """Close without calling anything (used once a choice was made)."""
        if self._done:
            return
        self._done = True
        self.host.close_overlay(self)
        self.closed.emit()

    def cancel(self) -> None:
        if self.busy or not self.dismissable:
            return
        self.on_cancel()
        self.dismiss()

    def on_cancel(self) -> None:
        pass

    def focus_default(self) -> None:
        order = nav.reading_order(nav.focusables(self), self)
        if order:
            order[0].setFocus(Qt.FocusReason.OtherFocusReason)

    def hints(self) -> list[tuple[list[Action], str]]:
        return [([Action.ACCEPT], self.accept_hint()), ([Action.BACK], "Close")]

    def accept_hint(self) -> str:
        """What A does on the focused control. The window refreshes the hint
        bar on every focus change."""
        return "Select"

    def handle_action(self, action: Action) -> bool:
        if action == Action.BACK:
            self.cancel()
            return True
        if action in (Action.PREV_TAB, Action.NEXT_TAB, Action.MENU):
            return True  # sections stay put while a sheet is open
        return False

    # --- painting and pointer ---
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(2, 5, 10, 196))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.sheet.geometry().contains(event.position().toPoint()):
            self.cancel()


def _tile(title: str, description: str, role: str, icon: str | None, on_click: Callable[[], None], *,
          size: str = "normal") -> Tile:
    t = Tile(title, description, icon, role=role, size=size)
    # Swallow clicked's ``checked`` argument: it would replace a callback's
    # default argument (``lambda k=key: ...``) with False.
    t.clicked.connect(lambda _checked=False: on_click())
    return t


class ConfirmSheet(Overlay):
    def __init__(self, host, title: str, text: str, choices: list[Choice],
                 on_choice: Callable[[str | None], None], *, default: str | None = None,
                 eyebrow: str = "") -> None:
        super().__init__(host, title, eyebrow=eyebrow)
        self._on_choice = on_choice
        self._default = default
        self.text = label(text, "secondary")
        self.body.addWidget(self.text)
        self.body.addSpacing(6)
        self.tiles: dict[str, Tile] = {}
        for key, name, description, role, icon in choices:
            tile = _tile(name, description, role, icon, lambda k=key: self.choose(k))
            self.tiles[key] = tile
            self.body.addWidget(tile)

    def accept_hint(self) -> str:
        focus = self.host.focusWidget()
        return focus.text() if focus in self.tiles.values() else "Select"

    def choose(self, key: str | None) -> None:
        if self._done:
            return
        self.dismiss()
        self._on_choice(key)

    def on_cancel(self) -> None:
        self._on_choice(None)

    def focus_default(self) -> None:
        tile = self.tiles.get(self._default) if self._default else None
        if tile is not None:
            tile.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            super().focus_default()


class DetailsSheet(Overlay):
    def __init__(self, host, title: str, text: str, *, eyebrow: str = "Details") -> None:
        super().__init__(host, title, eyebrow=eyebrow, width=860)
        self.text = label(text, "secondary", selectable=True)
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(0, 0, 8, 0)
        v.addWidget(self.text)
        v.addStretch(1)
        self.area = scroller(content)
        self.area.setMinimumHeight(min(360, self.text.sizeHint().height() + 20))
        self.body.addWidget(self.area, 1)
        row = QHBoxLayout()
        copy = _tile("Copy", "Put this text on the clipboard.", "normal", "clipboard", self._copy, size="compact")
        close = _tile("Close", "", "normal", "close", self.cancel, size="compact")
        row.addWidget(copy)
        row.addWidget(close)
        self.body.addLayout(row)

    def hints(self) -> list[tuple[list[Action], str]]:
        return [([Action.SCROLL_DOWN], "Scroll"), *super().hints()]

    def handle_action(self, action: Action) -> bool:
        if action in (Action.SCROLL_UP, Action.SCROLL_DOWN):
            nav.scroll_by(self.text, -0.8 if action == Action.SCROLL_UP else 0.8)
            return True
        return super().handle_action(action)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text.text())
        self.host.notify("Copied to the clipboard.", "ok")


# --- on-screen keyboard ------------------------------------------------------------------


class KeyButton(QAbstractButton):
    """One key of the on-screen keyboard. Typing on a real keyboard while a
    key has focus still types, so the sheet never fights a physical keyboard."""

    raw_keys = (Qt.Key.Key_Backspace, Qt.Key.Key_Space, Qt.Key.Key_Delete)
    text_sink = True

    def __init__(self, text: str, sheet: "KeyboardSheet", *, wide: float = 1.0, special: bool = False) -> None:
        super().__init__()
        self.sheet = sheet
        self.special = special
        self.setText(text)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setFixedSize(QSize(int(58 * wide + 8 * (wide - 1)), 54))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Backspace:
            self.sheet.backspace()
        elif event.key() == Qt.Key.Key_Delete:
            self.sheet.field.del_()
        elif event.matches(QKeySequence.StandardKey.Paste):
            self.sheet.paste()
        elif event.matches(QKeySequence.StandardKey.SelectAll):
            self.sheet.select_all()
        elif event.text() and event.text().isprintable():
            self.sheet.type_text(event.text())
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        lit = self.hasFocus() or self.underMouse()
        bg = theme.color("sunken" if self.isDown() else ("raised" if lit else "surface"))
        if self.isChecked():
            bg = theme.color("accent", 70)
        p.setBrush(bg)
        p.setPen(QPen(theme.color("accent", 140) if lit else theme.color("border"), 1.2))
        p.drawRoundedRect(r, 12, 12)
        p.setPen(theme.color("secondary" if self.special else "text"))
        p.setFont(theme.font(11.5 if self.special else 15, QFont.Weight.Bold))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text())


class CaretField(QLineEdit):
    """Keep the insertion point visible while controller focus is on a key."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.cursorPositionChanged.connect(lambda *_: self.update())
        self.selectionChanged.connect(self.update)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self.hasFocus() and not self.hasSelectedText():
            painter = QPainter(self)
            painter.setClipRect(self.contentsRect())
            painter.setPen(QPen(theme.color("accent_hi"), 2))
            cursor = self.cursorRect()
            painter.drawLine(cursor.topLeft(), cursor.bottomLeft())


class KeyboardSheet(Overlay):
    ROWS = ("1234567890", "qwertyuiop", "asdfghjkl:", "zxcvbnm.-/")
    SHIFTED = ("!@#$%^&*()", "QWERTYUIOP", "ASDFGHJKL:", "ZXCVBNM>_?")
    # The "#+=" layer: every printable ASCII symbol the letter rows don't have.
    SYMBOLS = ("1234567890", "!@#$%^&*()", "_=+[]{};'\"", "~`\\|,.<>?/")

    def __init__(self, host, title: str, prompt: str, on_done: Callable[[str], None], *,
                 text: str = "", placeholder: str = "",
                 validate: Callable[[str], str | None] | None = None, eyebrow: str = "Type") -> None:
        super().__init__(host, title, eyebrow=eyebrow, width=860)
        self._on_done = on_done
        self._validate = validate
        self.body.addWidget(label(prompt, "secondary"))
        self.field = CaretField(text)
        self.field.setPlaceholderText(placeholder)
        self.field.setProperty("noHalo", True)
        self.field.returnPressed.connect(self.done)
        self.body.addWidget(self.field)
        self.error = label("", "warning")
        self.error.setVisible(False)
        self.body.addWidget(self.error)

        grid = QGridLayout()
        grid.setSpacing(8)
        self.keys: list[KeyButton] = []
        for r, row in enumerate(self.ROWS):
            for c, ch in enumerate(row):
                key = KeyButton(ch, self)
                key.clicked.connect(lambda _=False, k=key: self.type_text(k.text()))
                grid.addWidget(key, r, c)
                self.keys.append(key)
        self.body.addLayout(grid)
        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.shift = KeyButton("Shift", self, wide=1.5, special=True)
        self.shift.setCheckable(True)
        self.shift.toggled.connect(self._relabel)
        self.symbols = KeyButton("#+=", self, wide=1.5, special=True)
        self.symbols.setCheckable(True)
        self.symbols.toggled.connect(self._relabel)
        space = KeyButton("Space", self, wide=2.5, special=True)
        space.clicked.connect(lambda: self.type_text(" "))
        back = KeyButton("Delete", self, wide=1.5, special=True)
        back.clicked.connect(self.backspace)
        clear = KeyButton("Clear", self, wide=1.5, special=True)
        clear.clicked.connect(self.field.clear)
        paste = KeyButton("Paste", self, wide=1.5, special=True)
        paste.clicked.connect(self.paste)
        done = KeyButton("Done", self, wide=1.5, special=True)
        done.clicked.connect(self.done)
        for key in (self.shift, self.symbols, space, back, clear, paste, done):
            bottom.addWidget(key)
        bottom.addStretch(1)
        self.body.addLayout(bottom)

        edit = QHBoxLayout()
        edit.setSpacing(8)
        self.cursor_left = KeyButton("←", self, special=True)
        self.cursor_right = KeyButton("→", self, special=True)
        self.cursor_left.clicked.connect(lambda: self.move_cursor(-1))
        self.cursor_right.clicked.connect(lambda: self.move_cursor(1))
        self.select = KeyButton("Select", self, wide=1.5, special=True)
        self.select.setCheckable(True)
        self.select.toggled.connect(self._on_select)
        self.select_all_key = KeyButton("Select all", self, wide=2, special=True)
        self.select_all_key.clicked.connect(self.select_all)
        start = KeyButton("Start", self, wide=1.5, special=True)
        end = KeyButton("End", self, wide=1.5, special=True)
        start.clicked.connect(lambda: self.field.home(self.select.isChecked()))
        end.clicked.connect(lambda: self.field.end(self.select.isChecked()))
        for key in (self.cursor_left, self.cursor_right, self.select, self.select_all_key, start, end):
            key.setFixedHeight(40)
            edit.addWidget(key)
        edit.addStretch(1)
        self.body.addLayout(edit)

    def focus_default(self) -> None:
        if self.host.router.mode == "gamepad":
            self.keys[0].setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.field.setFocus(Qt.FocusReason.OtherFocusReason)
            self.field.end(False)

    def hints(self) -> list[tuple[list[Action], str]]:
        focus = self.host.focusWidget()
        names = {self.cursor_left: "Cursor left", self.cursor_right: "Cursor right"}
        if focus is self.field:
            accept = "Done"
        elif isinstance(focus, KeyButton) and focus.special:
            accept = names.get(focus, focus.text())
        else:
            accept = "Type"
        if self.host.router.mode == "gamepad":
            return [([Action.ACCEPT], accept), ([Action.PREV_TAB, Action.NEXT_TAB], "Move cursor"),
                    ([Action.AUX], "Space"), ([Action.ALT], "Delete"), ([Action.MENU], "Done"),
                    ([Action.BACK], "Cancel")]
        return [([Action.ACCEPT], accept), ([Action.BACK], "Cancel")]

    def handle_action(self, action: Action) -> bool:
        focus = self.host.focusWidget()
        if action in (Action.PREV_TAB, Action.NEXT_TAB):
            self.move_cursor(-1 if action == Action.PREV_TAB else 1)
            return True
        if focus is self.field and action in (Action.LEFT, Action.RIGHT):
            self.move_cursor(-1 if action == Action.LEFT else 1)
            return True
        if action == Action.ACCEPT and focus is self.field:
            self.done()
            return True
        if action == Action.AUX:
            self.type_text(" ")
            return True
        if action == Action.ALT:
            self.backspace()
            return True
        if action == Action.MENU:
            self.done()
            return True
        if action == Action.UP and focus in self.keys[:10]:
            self.field.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return super().handle_action(action)

    def _relabel(self, *_args) -> None:
        symbols = self.symbols.isChecked()
        self.symbols.setText("abc" if symbols else "#+=")
        self.shift.setEnabled(not symbols)
        rows = self.SYMBOLS if symbols else (self.SHIFTED if self.shift.isChecked() else self.ROWS)
        text = "".join(rows)
        for key, ch in zip(self.keys, text):
            key.setText(ch)

    def move_cursor(self, direction: int) -> None:
        if direction < 0:
            self.field.cursorBackward(self.select.isChecked())
        else:
            self.field.cursorForward(self.select.isChecked())

    def _on_select(self, on: bool) -> None:
        self.select.setText("Selecting" if on else "Select")
        if not on:
            self.field.deselect()
        self.host.refresh_hints()

    def select_all(self) -> None:
        self.select.setChecked(False)
        self.field.selectAll()

    def type_text(self, text: str) -> None:
        self.field.insert(text)
        self.error.setVisible(False)

    def backspace(self) -> None:
        self.field.backspace()

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text().strip()
        if text:
            self.field.insert(text)
        else:
            self.host.notify("The clipboard is empty.", "warn")

    def value(self) -> str:
        return self.field.text().strip()

    def done(self) -> None:
        if self._done:
            return
        text = self.value()
        problem = self._validate(text) if self._validate else None
        if problem:
            self.error.setText(problem)
            self.error.setVisible(True)
            return
        self.dismiss()
        self._on_done(text)


# --- PIN pad --------------------------------------------------------------------------------


def normalize_pin(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())[:6]


class DigitCell(QWidget):
    """One wheel of the PIN pad. Up and down turn it, left and right move
    between wheels, digits typed on a keyboard fill it and move on."""

    raw_keys = (Qt.Key.Key_Backspace, Qt.Key.Key_Delete)
    text_sink = True

    def __init__(self, pad: "PinSheet", index: int) -> None:
        super().__init__()
        self.pad = pad
        self.index = index
        self.value = ""
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setProperty("noHalo", True)
        self.setFixedSize(72, 104)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set(self, value: str) -> None:
        self.value = value
        self.update()
        self.pad.changed()

    def handle_action(self, action: Action) -> bool:
        if action in (Action.UP, Action.DOWN):
            current = int(self.value) if self.value else (9 if action == Action.UP else 0)
            self.set(str((current + (1 if action == Action.UP else -1)) % 10))
            return True
        if action in (Action.LEFT, Action.RIGHT):
            return self.pad.move(self.index + (1 if action == Action.RIGHT else -1))
        if action == Action.ACCEPT:
            self.pad.advance(self.index)
            return True
        return False

    def keyPressEvent(self, event) -> None:  # noqa: N802
        text = event.text()
        if event.matches(QKeySequence.StandardKey.Paste):
            self.pad.set_pin(QGuiApplication.clipboard().text())
        elif text and text.isdigit():
            self.set(text)
            self.pad.move(self.index + 1)
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if self.value or event.key() == Qt.Key.Key_Delete:
                self.set("")
            elif self.pad.move(self.index - 1):
                self.pad.cells[self.index - 1].set("")
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        y = event.position().y()
        if y < self.height() * 0.3:
            self.handle_action(Action.UP)
        elif y > self.height() * 0.7:
            self.handle_action(Action.DOWN)

    def wheelEvent(self, event) -> None:  # noqa: N802
        self.handle_action(Action.UP if event.angleDelta().y() > 0 else Action.DOWN)

    def focusInEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().focusOutEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(4, 18, self.width() - 8, self.height() - 36)
        focused = self.hasFocus()
        p.setBrush(theme.color("raised" if focused else "sunken"))
        p.setPen(QPen(theme.color("accent_hi") if focused else theme.color("border"), 3 if focused else 1.5))
        p.drawRoundedRect(box, 14, 14)
        p.setPen(theme.color("text") if self.value else theme.color("muted"))
        p.setFont(theme.font(30, QFont.Weight.Black))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.value or "·")
        if focused:
            c = theme.color("accent")
            p.setPen(QPen(c, 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            mid = self.width() / 2
            p.drawPolyline([QPointF(mid - 8, 12), QPointF(mid, 4), QPointF(mid + 8, 12)])
            p.drawPolyline([QPointF(mid - 8, self.height() - 12), QPointF(mid, self.height() - 4),
                            QPointF(mid + 8, self.height() - 12)])


class PinSheet(Overlay):
    """Enter the PIN another machine shows, and its address when it wasn't
    found by the scan. ``on_done(announcement, pin)`` runs once both are valid."""

    def __init__(self, host, announcement: Announcement | None,
                 on_done: Callable[[Announcement, str], None]) -> None:
        name = announcement.name if announcement else "the machine with the mods"
        super().__init__(host, f"Pair with {announcement.name}" if announcement else "Pair by address",
                         eyebrow="Pairing", width=760)
        self._announcement = announcement
        self._on_done = on_done
        self._address = ""
        self.address_tile: Tile | None = None
        if announcement is None:
            self.address_tile = _tile(
                "Address of the machine with the mods", "Not set yet. Shown under \"Pair over network\" there.",
                "normal", "globe", self._edit_address)
            self.body.addWidget(self.address_tile)
        self.prompt = label(f"Enter the PIN shown on <b>{name}</b> under \"Pair over network\".", "secondary")
        self.body.addWidget(self.prompt)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cells = [DigitCell(self, i) for i in range(6)]
        for i, cell in enumerate(self.cells):
            if i == 3:
                row.addSpacing(18)
            row.addWidget(cell)
        row.addStretch(1)
        self.body.addLayout(row)
        self.hint = label("6 digits", "muted")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.hint)
        buttons = QHBoxLayout()
        self.pair_button = _tile("Pair", "", "primary", "link", self.submit, size="compact")
        cancel = _tile("Cancel", "", "normal", "close", self.cancel, size="compact")
        buttons.addWidget(self.pair_button)
        buttons.addWidget(cancel)
        self.body.addLayout(buttons)
        self.changed()

    def focus_default(self) -> None:
        if self.address_tile is not None and not self._address:
            self.address_tile.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self.cells[0].setFocus(Qt.FocusReason.OtherFocusReason)

    def hints(self) -> list[tuple[list[Action], str]]:
        focus = self.host.focusWidget()
        if isinstance(focus, DigitCell):
            # A on a wheel moves on, and pairs once all six digits are in.
            return [([Action.UP], "Change digit"), ([Action.ACCEPT], "Pair" if self.complete() else "Next digit"),
                    ([Action.BACK], "Cancel")]
        if focus is self.pair_button:
            accept = "Pair"
        elif focus is not None and focus is self.address_tile:
            accept = "Enter address"
        else:
            accept = "Select"
        return [([Action.ACCEPT], accept), ([Action.BACK], "Cancel")]

    # --- state ---
    def pin(self) -> str:
        return "".join(cell.value for cell in self.cells)

    def set_pin(self, text: str) -> None:
        digits = normalize_pin(text)
        for i, cell in enumerate(self.cells):
            cell.value = digits[i] if i < len(digits) else ""
            cell.update()
        self.changed()

    @property
    def shown_pin(self) -> str:
        pin = self.pin()
        return f"{pin[:3]} {pin[3:]}" if len(pin) > 3 else pin

    def complete(self) -> bool:
        ok = len(self.pin()) == 6
        if self._announcement is None:
            ok = ok and bool(self._address)
        return ok

    def changed(self) -> None:
        if not hasattr(self, "pair_button"):
            return
        self.pair_button.setEnabled(self.complete())
        filled = len(self.pin())
        self.hint.setText("Ready to pair" if self.complete() else (
            "Enter the address first" if filled == 6 else f"{filled} of 6 digits"))
        if self in self.host.overlays:
            self.host.refresh_hints()  # A turns from "Next digit" into "Pair"

    def move(self, index: int) -> bool:
        if 0 <= index < len(self.cells):
            self.cells[index].setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return False

    def advance(self, index: int) -> None:
        if self.complete():
            self.submit()
            return
        empty = next((i for i, c in enumerate(self.cells) if not c.value), None)
        if empty is not None:
            self.move(empty)
        elif not self.move(index + 1):
            self.pair_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _edit_address(self) -> None:
        def validate(text: str) -> str | None:
            try:
                Announcement.manual(text)
            except pairing_lan.PairError:
                return "Enter an address like 192.168.1.20 or 192.168.1.20:21029."
            return None

        KeyboardSheet(self.host, "Address", "The address shown on the machine with the mods, as host or host:port.",
                      self._set_address, text=self._address, placeholder="192.168.1.20",
                      validate=validate, eyebrow="Pair by address").open()

    def _set_address(self, text: str) -> None:
        self._address = text
        if self.address_tile is not None:
            self.address_tile.set_description(text or "Not set yet.")
        self.changed()
        self.cells[0].setFocus(Qt.FocusReason.OtherFocusReason)

    def set_address(self, text: str) -> None:
        self._set_address(text)

    def announcement(self) -> Announcement:
        """Raises ``pairing_lan.PairError`` for a malformed typed address."""
        if self._announcement is not None:
            return self._announcement
        return Announcement.manual(self._address)

    def submit(self) -> None:
        if not self.complete() or self._done:
            return
        try:
            target = self.announcement()
        except pairing_lan.PairError as exc:
            self.host.notify(f"⚠ {exc}")
            return
        pin = self.pin()
        self.dismiss()
        self._on_done(target, pin)


# --- folders --------------------------------------------------------------------------------


def _places() -> list[tuple[str, Path]]:
    places = [("Home", Path.home())]
    media = Path("/run/media")
    try:
        for user_dir in sorted(media.iterdir()):
            for mount in sorted(user_dir.iterdir()) if user_dir.is_dir() else []:
                if mount.is_dir():
                    places.append((mount.name, mount))
    except OSError:
        pass
    return places[:4]


class FolderSheet(Overlay):
    """Browse to a folder with the d-pad. Y goes up a level."""

    LIMIT = 300

    def __init__(self, host, title: str, start: Path | str | None, on_chosen: Callable[[Path], None], *,
                 confirm: str = "Use this folder", description: str = "") -> None:
        super().__init__(host, title, eyebrow="Choose a folder", width=900)
        self._on_chosen = on_chosen
        if description:
            self.body.addWidget(label(description, "secondary"))
        self.path_label = label("", "heading")
        self.body.addWidget(self.path_label)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.use = _tile(confirm, "", "primary", "check", self._use, size="compact")
        self.up = _tile("Up one level", "", "normal", "up", self.go_up, size="compact")
        actions.addWidget(self.use, 2)
        actions.addWidget(self.up, 1)
        for name, path in _places():
            t = _tile(name, "", "normal", "home" if path == Path.home() else "sd",
                      lambda p=path: self.navigate(p), size="compact")
            actions.addWidget(t, 1)
        self.body.addLayout(actions)
        self.list_host = QWidget()
        self.list = QVBoxLayout(self.list_host)
        self.list.setContentsMargins(0, 0, 10, 0)
        self.list.setSpacing(8)
        self.area = scroller(self.list_host)
        self.area.setMinimumHeight(320)
        self.body.addWidget(self.area, 1)
        self.entries: list[Tile] = []
        start_path = Path(start) if start else Path.home()
        while not start_path.is_dir() and start_path != start_path.parent:
            start_path = start_path.parent
        self.path = start_path
        self.navigate(start_path, focus=False)

    def hints(self) -> list[tuple[list[Action], str]]:
        focus = self.host.focusWidget()
        if focus in self.entries:
            accept = "Open"
        elif focus is self.use:
            accept = self.use.text()
        elif focus is self.up:
            accept = "Up one level"
        else:
            accept = "Go there"  # Home and SD card shortcuts
        return [([Action.ACCEPT], accept), ([Action.ALT], "Up one level"), ([Action.BACK], "Cancel")]

    def handle_action(self, action: Action) -> bool:
        if action == Action.ALT:
            self.go_up()
            return True
        return super().handle_action(action)

    def focus_default(self) -> None:
        (self.entries[0] if self.entries else self.use).setFocus(Qt.FocusReason.OtherFocusReason)

    def navigate(self, path: Path, *, focus: bool = True) -> None:
        self.path = path
        self.path_label.setText(str(path))
        self.entries = []
        clear_layout(self.list)
        try:
            folders = sorted(
                (e for e in os.scandir(path) if not e.name.startswith(".") and e.is_dir(follow_symlinks=True)),
                key=lambda e: e.name.lower(),
            )
        except OSError as exc:
            folders = []
            self.list.addWidget(label(f"Can't open this folder: {exc.strerror or exc}", "warning"))
        for entry in folders[: self.LIMIT]:
            tile = Tile(entry.name, "", "folder", size="compact", chevron=True)
            tile.clicked.connect(lambda _=False, p=Path(entry.path): self.navigate(p))
            self.list.addWidget(tile)
            tile.show()  # now, not on the next event loop pass, so it can take focus
            self.entries.append(tile)
        if not folders:
            self.list.addWidget(label("No folders here.", "muted"))
        elif len(folders) > self.LIMIT:
            self.list.addWidget(label(f"…and {len(folders) - self.LIMIT} more.", "muted"))
        self.list.addStretch(1)
        self.up.setEnabled(path.parent != path)
        self.area.verticalScrollBar().setValue(0)
        if focus:
            self.focus_default()

    def go_up(self) -> None:
        if self.path.parent != self.path:
            previous = self.path.name
            self.navigate(self.path.parent)
            for tile in self.entries:
                if tile.text() == previous:
                    tile.setFocus(Qt.FocusReason.OtherFocusReason)
                    nav.reveal(tile)
                    break

    def _use(self) -> None:
        path = self.path
        self.dismiss()
        self._on_chosen(path)


# --- pairing --------------------------------------------------------------------------------


class BeaconSheet(Overlay):
    """The host side of network pairing: one huge PIN to read across a room."""

    def __init__(self, host, pin: str, name: str, on_cancel: Callable[[], None]) -> None:
        super().__init__(host, "", eyebrow="Pair over network", width=820)
        self._cancelled = on_cancel
        self.pin_label = QLabel(f"{pin[:3]} {pin[3:]}")
        self.pin_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pin_label.setFont(theme.font(64, QFont.Weight.Black, spacing=10))
        self.pin_label.setStyleSheet(f"color: {theme.C['accent_hi']};")
        self.pin_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(self.pin_label)
        self.steps = label(
            "On the other machine, open <b>Sync</b>, choose <b>Copy from another machine</b>, "
            f"pick <b>{name}</b> from the list and enter this PIN.", "secondary")
        self.steps.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.steps)
        self.address = label("Starting…", "muted", selectable=True)
        self.address.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.address)
        self.waiting = label("Waiting for the other machine…", "eyebrow")
        self.waiting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body.addWidget(self.waiting)
        self.cancel_tile = _tile("Cancel pairing", "", "normal", "close", self.cancel, size="compact")
        self.body.addWidget(self.cancel_tile)

    def set_address(self, where: str) -> None:
        self.address.setText(f"Not listed there? Its address is {where}")

    def hints(self) -> list[tuple[list[Action], str]]:
        return [([Action.BACK], "Cancel pairing")]

    def on_cancel(self) -> None:
        self._cancelled()


class CodeSheet(Overlay):
    def __init__(self, host, code: str) -> None:
        super().__init__(host, "Pairing code", eyebrow="Sync", width=820)
        from modsync.ui.qr import pairing_pixmap

        row = QHBoxLayout()
        row.setSpacing(24)
        qr = QLabel()
        pixmap = pairing_pixmap(code, 220)
        if pixmap is not None:
            qr.setPixmap(pixmap)
        else:
            qr.setText("(install 'qrcode' to show a QR code)")
        qr.setFixedSize(236, 236)
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr.setStyleSheet("background: white; border-radius: 16px;")
        row.addWidget(qr, 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.addWidget(label("On another machine, choose \"Copy from another machine\", then "
                            "\"Paste a pairing code\", or scan this code.", "secondary"))
        self.code = label(code, "", selectable=True)
        self.code.setFont(QFont("monospace", 11))
        self.code.setStyleSheet(f"color: {theme.C['text']};")
        col.addWidget(self.code)
        col.addStretch(1)
        row.addLayout(col, 1)
        self.body.addLayout(row)
        buttons = QHBoxLayout()
        buttons.addWidget(_tile("Copy code", "", "primary", "clipboard", self._copy, size="compact"))
        buttons.addWidget(_tile("Close", "", "normal", "close", self.cancel, size="compact"))
        self.body.addLayout(buttons)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.code.text())
        self.host.notify("Pairing code copied to the clipboard.", "ok")
