"""The countdown before Steam's Play starts a game.

When Steam's Play opens ModSync and nothing needs a decision, the game starts
by itself after a few seconds (see ``launchhook.play_mode``). :class:`Countdown`
runs it for the page of the game Steam is starting. Meanwhile a sheet
sits over the dimmed window, the way ModSync's other sheets do, and says what
is about to start, when, and how to stop it. The hint bar stays clear of it
and names the same two choices.

It never takes focus: the page underneath keeps it, and the countdown decides
what each press means (``Countdown.handle``). A click or tap is handled here,
the moment it lands.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from modsync import launchhook
from modsync.ui import icons, theme
from modsync.ui.input import Action
from modsync.ui.widgets import ACTION_GLYPH, RADIUS

if TYPE_CHECKING:
    from modsync.games import Game

SHEET_W = 720  # the widest sheets ModSync shows
PAD_X, PAD_Y = 36, 32
LEGEND_H = 30  # the controller and key legends on the two choices
CHOICE_H = 92
METER_H = 10
SCRIM = QColor(0, 0, 0, 170)  # as behind every sheet


class CountdownCover(QWidget):
    """``stop`` and ``play_now`` are a click or tap; the page maps presses
    itself. ``starting`` keeps the sheet up, without the choices, while the
    launch is handed over."""

    stop = Signal()
    play_now = Signal()

    def __init__(self, host, game: str) -> None:
        super().__init__(host.shell)
        self.host = host
        self.router = host.router
        self.game = game
        self.seconds = 0
        self.fraction = 0.0  # of the wait gone by
        self.detail = ""
        self.starting = False
        self._hover = ""  # "stop" or "play" under the pointer
        self._pressed = ""
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        host.shell.installEventFilter(self)
        self.router.modeChanged.connect(self._restyle)
        self.router.controllersChanged.connect(self._restyle)
        self.hide()

    # --- what it shows ---
    def count(self, seconds: int, fraction: float, detail: str) -> None:
        if not self.isVisible():
            self.starting = False
            self._hover = self._pressed = ""
            self._place()
            self.show()
            self.raise_()
            self.host.refresh_hints()
        self.seconds, self.fraction, self.detail = seconds, max(0.0, min(1.0, fraction)), detail
        self.update()

    def start(self) -> None:
        """The wait is over (or Play now was chosen): the game is on its way."""
        self.starting = True
        self.fraction = 1.0
        self.unsetCursor()
        self.update()
        self.host.refresh_hints()

    def dismiss(self) -> None:
        if self.isVisible():
            self.hide()
            self.host.refresh_hints()

    def hints(self) -> list[tuple[list[Action], str]] | None:
        """What the hint bar shows while the sheet is up; None when it isn't."""
        if not self.isVisible():
            return None
        if self.starting:
            return []
        return [([Action.ACCEPT], "Play now"), ([Action.BACK], "Stop")]

    def _restyle(self, *_args) -> None:
        if self.isVisible():
            self.update()

    def _place(self) -> None:
        """Everything but the hint bar, as a sheet covers."""
        rect = self.host.shell.rect()
        rect.setBottom(rect.bottom() - self.host.hintbar.height())
        self.setGeometry(rect)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt API)
        if obj is self.host.shell and event.type() == QEvent.Type.Resize:
            self._place()
        return False

    # --- layout ---
    def _fonts(self):
        title = theme.font(22, QFont.Weight.Bold)
        digit = theme.font(120, QFont.Weight.Bold)
        detail = theme.font(12.5)
        return title, digit, detail

    def _sheet(self) -> QRectF:
        title, digit, detail = (QFontMetricsF(f) for f in self._fonts())
        h = (PAD_Y + title.height() + 20 + METER_H + 14 + detail.height() + PAD_Y)
        if not self.starting:
            h += digit.capHeight() + 36 + 28 + CHOICE_H
        w = min(SHEET_W, self.width() - 80)
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _choices(self) -> tuple[QRectF, QRectF]:
        sheet = self._sheet()
        top = sheet.bottom() - PAD_Y - CHOICE_H
        inner = sheet.adjusted(PAD_X, 0, -PAD_X, 0)
        gap = 10.0
        stop_w = (inner.width() - gap) * 0.56
        stop = QRectF(inner.left(), top, stop_w, CHOICE_H)
        play = QRectF(stop.right() + gap, top, inner.right() - stop.right() - gap, CHOICE_H)
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
                parts.append(("sep", "/", QFontMetricsF(theme.font(14)).horizontalAdvance("/") + 4))
            parts.append(("key", key, icons.keycap_width(key, LEGEND_H, font)))
        return parts

    @staticmethod
    def _legend_font() -> QFont:
        return theme.font(12.5)

    def _paint_legend(self, p: QPainter, parts, x: float, cy: float) -> float:
        font = self._legend_font()
        for kind, name, w in parts:
            rect = QRectF(x, cy - LEGEND_H / 2, w, LEGEND_H)
            if kind == "pad":
                icons.paint_glyph(p, name, rect, self.router.pad_style, font)
            elif kind == "sep":
                p.setPen(theme.color("muted"))
                p.setFont(theme.font(14))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            else:
                icons.paint_keycap(p, name, rect, font)
            x += w + 7
        return x

    def stop_hint(self) -> str:
        if self._pointer():
            return "Or tap anywhere else." if self.router.pointer == "touch" else "Or click anywhere else."
        if self.router.controller_present:
            return "Or any other button."
        return "Or any other key."

    # --- painting ---
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.fillRect(self.rect(), SCRIM)
        sheet = self._sheet()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("night"))
        p.drawRoundedRect(sheet, 6, 6)
        title_font, digit_font, detail_font = self._fonts()
        title_fm, digit_fm, detail_fm = (QFontMetricsF(f) for f in (title_font, digit_font, detail_font))
        left, width = sheet.left() + PAD_X, sheet.width() - 2 * PAD_X
        y = sheet.top() + PAD_Y

        # What is about to happen.
        p.setFont(title_font)
        p.setPen(theme.color("text"))
        title = f"Starting {self.game}…" if self.starting else f"{self.game} starts in"
        p.drawText(QRectF(left, y, width, title_fm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        y += title_fm.height() + 20

        # When: the seconds left, the largest thing on the screen.
        if not self.starting:
            number = str(self.seconds)
            ink = digit_fm.tightBoundingRect(number)  # the figure itself, without its side bearings
            baseline = y + digit_fm.capHeight()
            x = left - ink.left()
            p.setFont(digit_font)
            p.setPen(theme.color("text"))
            p.drawText(QPointF(x, baseline), number)
            p.setFont(theme.font(18, QFont.Weight.DemiBold))
            p.setPen(theme.color("secondary"))
            p.drawText(QPointF(x + ink.right() + 20, baseline), "second" if self.seconds == 1 else "seconds")
            y += digit_fm.capHeight() + 36

        # The wait, filling up.
        track = QRectF(left, y, width, METER_H)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("sunken"))
        p.drawRoundedRect(track, 3, 3)
        if self.fraction > 0:
            fill = QRectF(track.left(), track.top(), max(METER_H, track.width() * self.fraction), METER_H)
            p.setBrush(theme.color("accent_deep"))
            p.drawRoundedRect(fill, 3, 3)
        y += METER_H + 14

        # How.
        if self.detail:
            p.setFont(detail_font)
            p.setPen(theme.color("secondary"))
            p.drawText(QRectF(left, y, width, detail_fm.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       detail_fm.elidedText(self.detail, Qt.TextElideMode.ElideRight, width))

        if not self.starting:
            stop, play = self._choices()
            self._paint_choice(p, stop, "Stop", self.stop_hint(), Action.BACK,
                               hover=self._hover == "stop")
            self._paint_choice(p, play, "Play now", "Skip the wait.", Action.ACCEPT,
                               hover=self._hover == "play", pressed=self._pressed == "play")

    def _paint_choice(self, p: QPainter, rect: QRectF, title: str, hint: str, action: Action, *,
                      hover: bool, pressed: bool = False) -> None:
        # Neither is lit: a lit row is where A goes, and here A always plays.
        # Neither is blue either: the wait filling up is this sheet's blue.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("sunken" if pressed else "raised" if hover else "surface"))
        p.drawRoundedRect(rect, RADIUS, RADIUS)
        x = rect.left() + 20
        parts = self._legend(action)
        if parts:
            x = self._paint_legend(p, parts, x, rect.center().y()) + 12
        title_font = theme.font(18, QFont.Weight.Bold)
        hint_font = theme.font(12)
        title_fm, hint_fm = QFontMetricsF(title_font), QFontMetricsF(hint_font)
        text_w = rect.right() - 16 - x
        hint_lines = 2 if hint_fm.horizontalAdvance(hint) > text_w else 1
        block = title_fm.height() + 2 + hint_fm.lineSpacing() * hint_lines
        ty = rect.center().y() - block / 2
        p.setFont(title_font)
        p.setPen(theme.color("text"))
        p.drawText(QRectF(x, ty, text_w, title_fm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        p.setFont(hint_font)
        p.setPen(theme.color("secondary"))
        p.drawText(QRectF(x, ty + title_fm.height() + 2, text_w, hint_fm.lineSpacing() * hint_lines),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, hint)


COUNTDOWN_S = 5  # how long before Play presses itself


class Countdown(QObject):
    """Steam's Play set to "countdown", for the page of the game Steam is
    starting: once ``ready()`` says nothing needs a decision, the sheet counts
    down and ``start()`` runs what the page's Play does, unless anything is
    pressed first. Its state is "pending" until it can start, "running" under
    the sheet, "starting" once it has pressed Play, then "off" for good.

    The page calls :meth:`check` whenever what ``ready()`` reads may have
    changed, :meth:`settle` when the window's busy flag changes, and
    :meth:`handle` first in its ``handle_action``."""

    def __init__(self, page, game: "Game", name: str, *, play, ready: Callable[[], bool],
                 start: Callable[[], None], detail: Callable[[], str]) -> None:
        super().__init__(page)
        self.page = page
        self.host = host = page.host
        self.play = play  # the page's Play tile: counting down needs it there and enabled
        self.ready, self.start_play, self.detail = ready, start, detail
        steam = host.steam_launch
        self.state = ("pending" if steam is not None and steam.game.appid == game.appid and not steam.reason
                      and launchhook.play_mode() == "countdown" else "off")
        self._end = self._started = 0.0
        # A press that stopped it was made in this span (monotonic times). A
        # bumper tap made then acts on its release, which mustn't act as well.
        self._stopped_by: tuple[float, float] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)  # the sheet's meter fills smoothly
        self._timer.timeout.connect(self._tick)
        self.cover: CountdownCover | None = None
        # ModSync has been in front (under gamescope it may never say so)
        self._was_active = QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive
        if self.state == "off":
            return
        self.cover = CountdownCover(host, name)
        self.cover.stop.connect(lambda: self.stop())
        self.cover.play_now.connect(self.start_now)
        page.destroyed.connect(self.cover.deleteLater)  # it lives on the window, not the page
        # Focus may leave the page while the sheet is up (Play is disabled while
        # the launch is handed over), so the window asks here first too.
        host.action_guard = self.handle
        QGuiApplication.instance().applicationStateChanged.connect(self.on_app_state)

    @property
    def running(self) -> bool:
        return self.state == "running"

    def handle(self, action: Action) -> bool:
        """The sheet's choices: A plays now, anything else only stops it (B
        never goes on to ask about quitting). True when the press is used up."""
        if self.state == "running":
            if action == Action.ACCEPT:
                self.start_now()
            else:
                self.stop()
            return True
        if self.state == "starting":
            return True
        # Steam Input's bumpers are Ctrl and Alt taps, sent on release: the
        # press stopped the countdown already.
        tap = self.host.router.tap_press
        return (action in (Action.PREV_TAB, Action.NEXT_TAB) and tap is not None and self._stopped_by is not None
                and self._stopped_by[0] <= tap <= self._stopped_by[1])

    def _can(self) -> bool:
        host = self.host
        return (self.ready() and not host.busy and self.play.isVisible() and self.play.isEnabled()
                and host.page is self.page and host.scope() is host.chrome)

    def check(self) -> None:
        """Start the countdown once it can; stop it while it can't."""
        if self.state in ("off", "starting"):
            return
        if not self.host.untouched and self.state == "pending":
            self.state = "off"  # pressed before it started: the user has taken over
            return
        if not self._can():
            if self.state == "running":
                self.stop("pending")
            return
        if self.state == "pending":
            self.state = "running"
            self._started = time.monotonic()
            self._end = self._started + COUNTDOWN_S
            self._timer.start()
            self._tick()

    def settle(self) -> None:
        """The window's busy flag changed."""
        self.check()
        if self.state == "starting":
            # After the flag settles: a sync check hands over to the launch itself.
            QTimer.singleShot(0, self, self._launch_settled)

    def on_app_state(self, app_state) -> None:
        """Going to another window counts as a press: presses there never reach
        this one, so the countdown would start the game behind the user's back."""
        if app_state == Qt.ApplicationState.ApplicationActive:
            self._was_active = True
        elif self._was_active and self.state in ("pending", "running"):
            self.stop()

    def stop(self, state: str = "off") -> None:
        self._timer.stop()
        self.state = state
        if self.cover is not None:
            self.cover.dismiss()

    def start_now(self) -> None:
        """The wait is over, or Play now was chosen: what the Play tile does."""
        if self.state != "running":
            return
        self._timer.stop()
        self.state = "starting"
        self.cover.start()
        self.start_play()
        self._launch_settled()

    def _launch_settled(self) -> None:
        """Back to the page once the launch has stopped short of Steam (a
        question, or an error to read); the window closes otherwise."""
        steam = self.host.steam_launch
        if self.state == "starting" and not self.host.launching and (steam is None or steam.decision is None):
            self.stop()
            # Disabled meanwhile, Play lost focus to whatever Qt chose; no press
            # under the sheet moved it on purpose.
            if self.host.scope() is self.host.chrome and self.play.isEnabled():
                self.play.setFocus()

    def _tick(self) -> None:
        if self.state != "running":
            return
        router = self.host.router
        if router.last_input >= self._started or (
                not self._was_active and router.last_pad_press >= self._started):
            # Any press stays here; A on Play has started it already. While ModSync
            # has never said it's in front (gamescope may not), a controller press
            # may be meant for it, so that stays too.
            self._stopped_by = (self._started, router.last_input)
            self.stop()
            return
        if self._was_active and QGuiApplication.applicationState() != Qt.ApplicationState.ApplicationActive:
            self.stop()  # gone to another window
            return
        if not self._can():
            self.stop("pending")
            return
        left = self._end - time.monotonic()
        if left > 0:
            self.cover.count(int(left) + 1, 1 - left / COUNTDOWN_S, self.detail())
            return
        self.start_now()
