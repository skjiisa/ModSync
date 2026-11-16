"""The countdown before Steam's Play starts a game.

When Steam's Play opens ModSync and nothing needs a decision, the game starts
by itself after a few seconds (see ``launchhook.play_mode``). This cover takes
over the whole window meanwhile, so it's plain what is about to start, when,
and how to stop it. It never takes focus: the page underneath keeps it, and
decides what each press means (see ``HomePage.handle_action``). A click or tap
on the cover is handled here, the moment it lands.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetricsF, QPainter
from PySide6.QtWidgets import QWidget

from modsync.ui import icons, theme
from modsync.ui.input import Action
from modsync.ui.widgets import ACTION_GLYPH, RADIUS

LEGEND_H = 34  # the controller and key legends on the two choices
CHOICE_H = 124
METER_H = 10


class CountdownCover(QWidget):
    """``stop`` and ``play_now`` are a click or tap on the cover; the page
    maps presses itself. ``starting`` keeps the cover up, without the
    choices, while the launch is handed over."""

    stop = Signal()
    play_now = Signal()

    def __init__(self, parent: QWidget, router, game: str) -> None:
        super().__init__(parent)
        self.router = router
        self.game = game
        self.seconds = 0
        self.fraction = 0.0  # of the wait gone by
        self.detail = ""
        self.starting = False
        self._hover = ""  # "stop" or "play" under the pointer
        self._pressed = ""
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        parent.installEventFilter(self)
        from modsync.ui.main_window import app_mark  # the window's own module imports the pages

        # The top bar's mark, where it is there: the window is still ModSync.
        self.mark = app_mark(32)
        self.mark.setParent(self)
        self.mark.move(24, 14)
        router.modeChanged.connect(self._restyle)
        router.controllersChanged.connect(self._restyle)
        self.hide()

    # --- what it shows ---
    def count(self, seconds: int, fraction: float, detail: str) -> None:
        if not self.isVisible():
            self.starting = False
            self._hover = self._pressed = ""
            self.setGeometry(self.parentWidget().rect())
            self.show()
            self.raise_()
        self.seconds, self.fraction, self.detail = seconds, max(0.0, min(1.0, fraction)), detail
        self.update()

    def start(self) -> None:
        """The wait is over (or Play now was chosen): the game is on its way."""
        self.starting = True
        self.fraction = 1.0
        self.unsetCursor()
        self.update()

    def _restyle(self, *_args) -> None:
        if self.isVisible():
            self.update()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt API)
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        return False

    # --- layout ---
    def _frame(self) -> QRectF:
        """The content area: generous margins, wider on a wide window."""
        side = max(56.0, min(120.0, self.width() * 0.07))
        return QRectF(side, 0, self.width() - 2 * side, self.height())

    def _choices(self) -> tuple[QRectF, QRectF]:
        f = self._frame()
        bottom = self.height() - max(48.0, self.height() * 0.07)
        top = bottom - CHOICE_H
        gap = 12.0
        stop_w = (f.width() - gap) * 0.58
        stop = QRectF(f.left(), top, stop_w, CHOICE_H)
        play = QRectF(stop.right() + gap, top, f.right() - stop.right() - gap, CHOICE_H)
        return stop, play

    def _choice_at(self, pos) -> str:
        if self.starting:
            return ""
        stop, play = self._choices()
        if play.contains(pos):
            return "play"
        return "stop" if stop.contains(pos) else ""

    # --- pointer: a click or tap anywhere stops it, except on Play now ---
    def mousePressEvent(self, event) -> None:  # noqa: N802
        event.accept()
        if self.starting:
            return
        if self._choice_at(event.position()) == "play":
            self._pressed = "play"
            self.update()
            self.play_now.emit()
        else:
            self.stop.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        hover = self._choice_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor)
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover:
            self._hover = ""
            self.update()

    # --- legends ---
    def _pointer(self) -> bool:
        return self.router.mode == "mouse"

    def _legend(self, action: Action) -> list[tuple[str, str, float]]:
        """The controller button and the keyboard key for ``action``, as the
        hint bar prints them: ("pad", glyph, w), ("sep", "/", w), ("key", label, w)."""
        if self._pointer():
            return []
        font = self._legend_font()
        parts: list[tuple[str, str, float]] = []
        if self.router.controller_present and self.router.pad_sends(action):
            glyph = ACTION_GLYPH[action]
            parts.append(("pad", glyph, icons.glyph_width(glyph, self.router.pad_style, LEGEND_H, font)))
        key = self.router.key_label(action)
        if key:
            if parts:
                parts.append(("sep", "/", QFontMetricsF(theme.font(15)).horizontalAdvance("/") + 4))
            parts.append(("key", key, icons.keycap_width(key, LEGEND_H, font)))
        return parts

    @staticmethod
    def _legend_font() -> QFont:
        return theme.font(14)

    def _paint_legend(self, p: QPainter, parts, x: float, cy: float, on: str) -> float:
        font = self._legend_font()
        for kind, name, w in parts:
            rect = QRectF(x, cy - LEGEND_H / 2, w, LEGEND_H)
            if kind == "pad":
                icons.paint_glyph(p, name, rect, self.router.pad_style, font, on=on)
            elif kind == "sep":
                p.setPen(theme.color("on_accent" if on == "accent" else "muted"))
                p.setFont(theme.font(15))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            else:
                icons.paint_keycap(p, name, rect, font, on=on)
            x += w + 8
        return x

    def stop_hint(self) -> str:
        if self._pointer():
            return "Or tap anywhere else." if self.router.pointer == "touch" else "Or click anywhere else."
        if self.router.controller_present:
            return "Any button except A stops it too. ModSync stays open."
        return "Any key except Enter stops it too. ModSync stays open."

    # --- painting ---
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.fillRect(self.rect(), theme.color("night"))
        f = self._frame()
        stop, play = self._choices()
        # Everything above the choices sits in the space they leave, a little
        # above its middle so the number reads first.
        p.setFont(theme.font(14, QFont.Weight.Bold))
        p.setPen(theme.color("text"))
        p.drawText(QRectF(66, 0, 300, 60), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "ModSync")
        title_font = theme.font(26, QFont.Weight.Bold)
        digit_font = theme.font(180, QFont.Weight.Bold)
        title_fm, digit_fm = QFontMetricsF(title_font), QFontMetricsF(digit_font)
        detail_font = theme.font(14)
        detail_fm = QFontMetricsF(detail_font)
        digit_h = 0.0 if self.starting else digit_fm.capHeight() + 48  # the number, and the room under it
        block = title_fm.height() + 36 + digit_h + METER_H + 22 + detail_fm.height()
        room_top, room_bottom = 60.0, self.height() - 60 if self.starting else stop.top()
        top = max(room_top + 16, room_top + (room_bottom - room_top - block) * 0.5)

        # What is about to happen.
        p.setFont(title_font)
        p.setPen(theme.color("text"))
        title = f"Starting {self.game}…" if self.starting else f"{self.game} starts in"
        p.drawText(QRectF(f.left(), top, f.width(), title_fm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        y = top + title_fm.height() + 36

        # When: the seconds left, as large as the screen allows.
        if not self.starting:
            number = str(self.seconds)
            ink = digit_fm.tightBoundingRect(number)  # the figure itself, without its side bearings
            baseline = y + digit_fm.capHeight()
            x = f.left() - ink.left()
            p.setFont(digit_font)
            p.setPen(theme.color("text"))
            p.drawText(QPointF(x, baseline), number)
            p.setFont(theme.font(20, QFont.Weight.DemiBold))
            p.setPen(theme.color("secondary"))
            p.drawText(QPointF(x + ink.right() + 24, baseline), "second" if self.seconds == 1 else "seconds")
        y += digit_h

        # The wait, filling up.
        track = QRectF(f.left(), y, f.width(), METER_H)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("sunken"))
        p.drawRoundedRect(track, 3, 3)
        if self.fraction > 0:
            fill = QRectF(track.left(), track.top(), max(METER_H, track.width() * self.fraction), METER_H)
            p.setBrush(theme.color("accent_deep"))
            p.drawRoundedRect(fill, 3, 3)
        y += METER_H + 22

        # How.
        if self.detail:
            p.setFont(detail_font)
            p.setPen(theme.color("secondary"))
            p.drawText(QRectF(f.left(), y, f.width() * 0.8, detail_fm.height() * 2),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                       self.detail)

        if not self.starting:
            self._paint_choice(p, stop, "Stop", self.stop_hint(), Action.BACK,
                               hover=self._hover == "stop")
            self._paint_choice(p, play, "Play now", "Skip the wait.", Action.ACCEPT,
                               hover=self._hover == "play", pressed=self._pressed == "play")

    def _paint_choice(self, p: QPainter, rect: QRectF, title: str, hint: str, action: Action, *,
                      hover: bool, pressed: bool = False) -> None:
        # Neither is lit: a lit row is where A goes, and here A always plays.
        # Neither is blue either: the wait filling up is this screen's blue.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("sunken" if pressed else "raised" if hover else "surface"))
        p.drawRoundedRect(rect, RADIUS, RADIUS)
        x = rect.left() + 28
        parts = self._legend(action)
        if parts:
            x = self._paint_legend(p, parts, x, rect.center().y(), "") + 14
        title_font = theme.font(22, QFont.Weight.Bold)
        hint_font = theme.font(12.5)
        title_fm, hint_fm = QFontMetricsF(title_font), QFontMetricsF(hint_font)
        text_w = rect.right() - 24 - x
        hint_lines = 2 if hint_fm.horizontalAdvance(hint) > text_w else 1
        block = title_fm.height() + 4 + hint_fm.lineSpacing() * hint_lines
        ty = rect.center().y() - block / 2
        p.setFont(title_font)
        p.setPen(theme.color("text"))
        p.drawText(QRectF(x, ty, text_w, title_fm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        p.setFont(hint_font)
        p.setPen(theme.color("secondary"))
        p.drawText(QRectF(x, ty + title_fm.height() + 4, text_w, hint_fm.lineSpacing() * hint_lines),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, hint)
