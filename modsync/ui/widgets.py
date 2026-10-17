"""The building blocks every screen is made of.

Everything that can be chosen is a ``Tile``: a flat row with a title, the
description a tooltip would otherwise hide (a controller can't hover) and an
optional value on the right. A focused row lights up white and shows the
button that chooses it. The rest dresses the window: toasts, the section
tabs and the hint bar along the bottom that shows which button does what.
"""

from __future__ import annotations

from typing import Callable

import shiboken6

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QScroller,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from modsync.ui import icons, theme
from modsync.ui.input import Action

RADIUS = 4  # rows, buttons, keys and sheets all share the Deck's small corner

# --- small helpers -------------------------------------------------------------------

def discard(w: QWidget | None) -> None:
    """Hide and delete a widget that was taken out of a layout.

    ``QLayout.takeAt`` drops the reference that kept the widget's Python half
    alive, so it can be collected while the C++ widget waits for
    ``deleteLater``. A visible widget in that state still gets paint events,
    and ``paintEvent`` is pure virtual on buttons. Hidden, it gets none.
    (Holding the wrapper until Qt deletes the widget is worse: once the
    address is reused, freeing the stale wrapper unmaps the new widget.)"""
    if w is None:
        return
    w.hide()
    w.deleteLater()


def clear_layout(layout) -> None:
    """Empty a layout, discarding its widgets and nested layouts' widgets."""
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            discard(item.widget())
        elif item.layout() is not None:
            clear_layout(item.layout())


def label(text: str = "", role: str = "", *, wrap: bool = True, selectable: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    if role:
        theme.role(lab, role)
    if selectable:
        lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lab


def pill(text: str = "", tone: str = "off") -> QLabel:
    """A short state word in its tone's color, such as "Needs 1.5.97"."""
    lab = QLabel(text)
    lab.setProperty("pill", tone)
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return lab


def set_pill(lab: QLabel, text: str, tone: str) -> None:
    lab.setText(text)
    lab.setVisible(bool(text))
    if lab.property("pill") != tone:
        lab.setProperty("pill", tone)
        theme.repolish(lab)


class Panel(QFrame):
    """A flat block a little lighter than the ground, for readouts that aren't
    choices (the installed version, the devices in sync)."""

    def __init__(self, parent: QWidget | None = None, *, margins: int = 20, spacing: int = 12) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(margins, margins, margins, margins)
        self.layout_.setSpacing(spacing)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.layout_.addWidget(widget, stretch)
        return widget


def _router():
    from modsync.ui.input import InputRouter

    return InputRouter._instance


def shows_focus(w: QWidget) -> bool:
    """Whether to draw ``w`` as focused. With a mouse or touch screen, the
    focused tile is only where Qt's keyboard focus happens to sit, and
    lighting it would look like a selection the user never made."""
    router = _router()
    return w.hasFocus() and (router is None or router.mode != "mouse")


def glyph_style() -> str:
    router = _router()
    return router.glyph_style if router is not None else "xbox"


def sync_lit(w: QWidget | None) -> None:
    """Re-light ``w`` after the input device changed (a mouse unlights it)."""
    if w is not None and shiboken6.isValid(w) and hasattr(w, "sync_lit"):
        w.sync_lit()


def restyle_rows(root: QWidget) -> None:
    """The input device changed: rows re-measure their legend slot."""
    for end in root.findChildren(TileEnd):
        end.updateGeometry()
        end.update()


def touch_scroll(area) -> None:
    """Let a finger (or the Steam Frame's pointer, which arrives as touch)
    drag the content with momentum. Taps still click what's under them."""
    QScroller.grabGesture(area.viewport(), QScroller.ScrollerGestureType.TouchGesture)


def scroller(content: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    touch_scroll(area)
    return area


class Icon(QWidget):
    """A line icon. ``tone`` names a theme color; a lit tile paints it dark."""

    def __init__(self, name: str, size: int = 24, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.tone = "secondary"
        self.lit = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set(self, name: str, tone: str | None = None) -> None:
        self.name = name
        if tone:
            self.tone = tone
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            fg = theme.color("disabled")
        elif self.lit and self.tone != "on_accent":
            fg = theme.color("on_focus")
        else:
            fg = theme.color(self.tone)
        icons.paint_icon(p, self.name, QRectF(self.rect()), fg, weight=2.0)


class Dot(QWidget):
    """A small status light: green ready, amber needs you, grey not yet."""

    def __init__(self, tone: str = "off", parent: QWidget | None = None, *, size: int = 10) -> None:
        super().__init__(parent)
        self.tone = tone
        self.lit = False
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def set_tone(self, tone: str) -> None:
        self.tone = tone
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        if self.tone in ("ok", "warn", "danger", "info", "busy"):
            p.setBrush(theme.tone_color(self.tone, lit=self.lit))
        else:
            p.setBrush(theme.color("on_focus_dim" if self.lit else "border"))
        p.drawEllipse(QRectF(self.rect()))


# --- tiles ---------------------------------------------------------------------------


class TileEnd(QWidget):
    """The right edge of a row: a chevron when it leads somewhere, and the
    button that chooses it while it is lit."""

    def __init__(self, tile: "Tile") -> None:
        super().__init__(tile)
        self.tile = tile
        self.chevron = False
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _glyph_width(self) -> float:
        return icons.glyph_width("accept", glyph_style(), 26, theme.font(11))

    def sizeHint(self) -> QSize:  # noqa: N802
        # A mouse or a finger never lights a row, so only the chevron needs room.
        router = _router()
        legend = 0.0 if router is not None and router.mode == "mouse" else self._glyph_width()
        return QSize(int(max(22, legend)) + 2, 26)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        tile = self.tile
        if tile.lit and tile.isEnabled():
            w = self._glyph_width()
            rect = QRectF(self.width() - w - 1, (self.height() - 26) / 2, w, 26)
            icons.paint_glyph(p, "accept", rect, glyph_style(), theme.font(11),
                              on="accent" if tile.property("tileRole") == "primary" else "focus")
            return
        if self.chevron:
            fg = theme.color("on_accent" if tile.property("tileRole") == "primary" else "muted")
            if not tile.isEnabled():
                fg = theme.color("disabled")
            icons.paint_icon(p, "chevron", QRectF(self.width() - 22, (self.height() - 22) / 2, 22, 22), fg,
                             weight=2.0)


class Tile(QAbstractButton):
    """A focusable row: title, description, an optional value on the right.

    ``role`` is "normal", "primary" (filled with the accent) or "danger".
    ``size`` is "normal", "compact", "choice" (a tall card) or "hero". Only
    hero and choice tiles show their icon; a list of rows reads better
    without one on every line."""

    SHOWS_ICON = ("hero", "choice")

    def __init__(
        self,
        title: str,
        description: str = "",
        icon: str | None = None,
        *,
        role: str = "normal",
        size: str = "normal",
        chevron: bool = False,
        status: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setProperty("tileRole", role)
        self.setProperty("tileSize", size)
        self.setProperty("ownFocus", True)
        self.setProperty("lit", False)

        vertical = size == "choice"
        outer = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        pad = {"compact": (18, 11, 14, 11), "hero": (26, 20, 20, 20), "choice": (24, 22, 24, 22)}.get(
            size, (18, 14, 14, 14))
        outer.setContentsMargins(*pad)
        outer.setSpacing(14 if not vertical else 12)
        # Children get their parent up front: a parentless widget made visible
        # becomes a window of its own, however briefly.
        self.lead = QWidget(self)  # the status dot of a StatusRow sits here
        self.lead.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lead = QHBoxLayout(self.lead)
        lead.setContentsMargins(0, 0, 0, 0)
        lead.setSpacing(0)
        self.icon = Icon(icon or "chevron", {"hero": 30, "choice": 32}.get(size, 24), self.lead)
        if role == "primary":
            self.icon.tone = "on_accent"
        elif role == "danger":
            self.icon.tone = "danger"
        lead.addWidget(self.icon)
        self.lead.setVisible(icon is not None and size in self.SHOWS_ICON)
        # A row with a state gets a status dot before its title; the value word
        # on the right takes the same tone.
        self.dot: Dot | None = None
        if status:
            self.dot = Dot("off", self.lead)
            lead.addWidget(self.dot)
            self.icon.hide()
            self.lead.setFixedWidth(12)
            self.lead.setVisible(True)
        outer.addWidget(self.lead, 0, (Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft) if vertical
                        else Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = QLabel(title, self)
        self.title.setObjectName("tileTitle")
        self.title.setWordWrap(True)
        self.desc = QLabel(description, self)
        self.desc.setObjectName("tileDesc")
        self.desc.setWordWrap(True)
        self.desc.setVisible(bool(description))
        for lab in (self.title, self.desc):
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            text.addWidget(lab)
        if vertical:
            text.addStretch(1)
        outer.addLayout(text, 1)

        self.badge = QLabel(self)  # the value on the right: "Ready", "1.6.1170", "Off"
        self.badge.setObjectName("tileValue")
        self.badge.setProperty("tone", "off")
        self.badge.setVisible(False)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        outer.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.end = TileEnd(self)
        self.end.chevron = chevron
        outer.addWidget(self.end, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        super().setText(title)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        sp.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    # --- content ---
    def setText(self, text: str) -> None:  # noqa: N802 (Qt API)
        super().setText(text)
        self.title.setText(text)

    def set_description(self, text: str) -> None:
        self.desc.setText(text)
        self.desc.setVisible(bool(text))

    def set_badge(self, text: str, tone: str = "off") -> None:
        self.badge.setText(text)
        self.badge.setVisible(bool(text))
        if self.dot is not None:
            self.dot.set_tone(tone)
        if self.badge.property("tone") != tone:
            self.badge.setProperty("tone", tone)
            theme.repolish(self.badge)

    def set_icon(self, name: str, tone: str | None = None) -> None:
        self.icon.set(name, tone)
        self.lead.setVisible(self.dot is not None or self.property("tileSize") in self.SHOWS_ICON)

    def set_chevron(self, on: bool) -> None:
        self.end.chevron = on
        self.end.update()

    def set_role(self, role: str) -> None:
        if self.property("tileRole") == role:
            return
        self.setProperty("tileRole", role)
        self.icon.tone = "on_accent" if role == "primary" else ("danger" if role == "danger" else "secondary")
        theme.repolish(self)

    @property
    def description(self) -> str:
        return self.desc.text()

    # --- focus: a lit row ---
    @property
    def lit(self) -> bool:
        return bool(self.property("lit"))

    def sync_lit(self) -> None:
        lit = shows_focus(self)
        if lit == self.lit:
            return
        self.setProperty("lit", lit)
        self.icon.lit = lit
        for dot in self.findChildren(Dot):
            dot.lit = lit
        self.end.updateGeometry()  # the legend is wider for a keyboard's Enter
        theme.repolish(self)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.sync_lit()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.sync_lit()

    # --- geometry: the layout knows best ---
    def sizeHint(self) -> QSize:  # noqa: N802
        return self.layout().sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.layout().minimumSize()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self.layout().totalHeightForWidth(width)

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.EnabledChange:
            self.icon.update()
            self.end.update()
        super().changeEvent(event)

    # A press that travels is a scroll, not a choice. With touch scrolling the
    # tile moves along with the finger, so the release still lands on it and
    # Qt would count a click; compare screen positions instead.
    DRAG_CANCEL_PX = 16

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._press_at = event.globalPosition()
        super().mousePressEvent(event)

    def _travelled(self, event) -> bool:
        start = getattr(self, "_press_at", None)
        return start is not None and (event.globalPosition() - start).manhattanLength() > self.DRAG_CANCEL_PX

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._travelled(event):
            self.setDown(False)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._travelled(event):
            self._press_at = None
            self.setDown(False)
            event.accept()
            return
        self._press_at = None
        super().mouseReleaseEvent(event)

    # --- painting ---
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        radius = RADIUS + (2 if self.property("tileSize") == "hero" else 0)
        primary = self.property("tileRole") == "primary"
        lit = self.lit and shows_focus(self)
        down = self.isDown()
        hover = self.underMouse() and not lit
        if not self.isEnabled():
            p.setOpacity(0.5)
        p.setPen(Qt.PenStyle.NoPen)
        if primary:
            fill = theme.color("accent_deep")
            if down:
                fill = fill.darker(112)
            elif hover:
                fill = fill.lighter(112)
            p.setBrush(fill)
            p.drawRoundedRect(r, radius, radius)
            if lit:  # the blue stays; a white ring says it is the one selected
                p.setPen(QPen(theme.color("focus"), 3))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(1.5, 1.5, -1.5, -1.5), radius, radius)
            return
        if lit:
            fill = theme.color("focus_pressed" if down else "focus")
        else:
            fill = theme.color("sunken" if down else ("raised" if hover else "surface"))
        p.setBrush(fill)
        p.drawRoundedRect(r, radius, radius)


class HeroTile(Tile):
    """The one big action on a screen (Play, Continue, Install MO2). While a
    repair is recommended it steps down to a plain row, so the screen has one
    blue action."""

    def __init__(self, title: str, description: str = "", icon: str = "play", parent: QWidget | None = None) -> None:
        super().__init__(title, description, icon, role="primary", size="hero", parent=parent)
        self.setMinimumHeight(96)

    def set_role(self, role: str) -> None:
        if self.property("tileRole") == role:
            return
        primary = role == "primary"
        self.setProperty("tileSize", "hero" if primary else "normal")
        self.lead.setVisible(primary)
        self.setMinimumHeight(96 if primary else 0)
        self.layout().setContentsMargins(*((26, 20, 20, 20) if primary else (18, 14, 14, 14)))
        super().set_role(role)
        self.updateGeometry()


class StatusRow(Tile):
    """One line of the readiness list: a status dot, what it is, its state on
    the right, and a way there."""

    def __init__(self, title: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(title, "", None, size="compact", chevron=True, status=True, parent=parent)
        self.base_icon = icon

    def set_state(self, value: str, tone: str, detail: str = "") -> None:
        self.set_badge(value, tone)
        self.set_description(detail)


# --- big readouts ------------------------------------------------------------------------


class SyncMeter(QWidget):
    """How much of the shared folder is here, as a flat bar. The words next to
    it say whether Syncthing is still working."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(8)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._value = 0.0
        self._target = 0.0
        self.caption = ""
        self.busy = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(500)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(320, 8)

    def set_value(self, percent: float, caption: str = "", *, busy: bool = False) -> None:
        self.caption = caption
        self.busy = busy
        # Both ends as floats: QVariantAnimation can't interpolate int to float.
        target = float(max(0.0, min(100.0, percent)))
        if target != self._target:
            self._target = target
            self._anim.stop()
            self._anim.setStartValue(float(self._value))
            self._anim.setEndValue(target)
            self._anim.start()
        self.update()

    @property
    def value(self) -> float:
        return self._target

    def _on_anim(self, v) -> None:
        if v is None:  # emitted while start and end are being replaced
            return
        self._value = float(v)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        bar = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("sunken"))
        p.drawRoundedRect(bar, 3, 3)
        if self._value > 0:
            p.setBrush(theme.color("accent_deep"))
            p.drawRoundedRect(QRectF(bar.left(), bar.top(), bar.width() * self._value / 100, bar.height()), 3, 3)


# --- page furniture ------------------------------------------------------------------------


class PageHeader(QWidget):
    """A page title, with a line under it only when the title needs one."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.title = label(title, "title")
        self.subtitle = label(subtitle, "secondary")
        v.addWidget(self.title)
        v.addWidget(self.subtitle)
        self.subtitle.setVisible(bool(subtitle))

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


# --- window chrome ---------------------------------------------------------------------------


class Backdrop(QWidget):
    """The window ground: one flat color."""

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(event.rect(), theme.color("night"))


class FocusHalo(QWidget):
    """A white outline that slides to whatever has focus, for controls that
    don't light up themselves (rows and keys do).

    It is one overlay over the whole window that never takes the mouse; it
    follows its target through scrolling and layout changes on a frame timer."""

    PAD = 4

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.target: QWidget | None = None
        self.enabled = True
        self._rect = QRectF()
        self._shown = QRectF()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        # 30 fps is enough to follow scrolling and layout changes.
        self._tracker = QTimer(self)
        self._tracker.setInterval(33)
        self._tracker.timeout.connect(self._track)
        self._tracker.start()

    def follow(self, widget: QWidget | None) -> None:
        if widget is not None and (widget.property("noHalo") or widget.property("ownFocus")
                                   or widget is self.parent()):
            widget = None
        self.target = widget
        rect = self._target_rect()
        if rect is None:
            old = self._shown
            self._shown = QRectF()
            self._rect = QRectF()
            if not old.isNull():
                self._dirty(old)
            return
        if self._shown.isNull():
            self._shown = rect
            self._rect = rect
            self._dirty(QRectF())
            return
        self._rect = rect
        self._anim.stop()
        self._anim.setStartValue(self._shown)
        self._anim.setEndValue(rect)
        self._anim.start()

    def _target_rect(self) -> QRectF | None:
        w = self.target
        if w is None or not self.enabled:
            return None
        try:
            if not w.isVisible() or w.window() is not self.window():
                return None
        except RuntimeError:  # deleted under us
            self.target = None
            return None
        root = self.parentWidget()
        if not root.isAncestorOf(w):
            return None
        visible = QRect(w.mapTo(root, QPoint(0, 0)), w.size())
        # Clip to every ancestor (scroll viewports especially), so a
        # half-scrolled control is framed only where it shows.
        a = w.parentWidget()
        while a is not None and a is not root:
            visible = visible.intersected(QRect(a.mapTo(root, QPoint(0, 0)), a.size()))
            a = a.parentWidget()
        if visible.isEmpty():
            return None
        return QRectF(visible).adjusted(-self.PAD, -self.PAD, self.PAD, self.PAD)

    def _on_anim(self, v) -> None:
        if v is None:
            return
        old = self._shown
        self._shown = QRectF(v)
        self._dirty(old)

    def _track(self) -> None:
        # Nothing to follow (or to spend the Deck's battery on) while the game
        # or another app is in front.
        if not self.isVisible() or not self.window().isActiveWindow():
            return
        rect = self._target_rect()
        if rect is None:
            if not self._shown.isNull():
                old = self._shown
                self._shown = QRectF()
                self._dirty(old)
            return
        if self._anim.state() == QVariantAnimation.State.Running:
            if rect != self._rect:
                self._rect = rect
                self._anim.setEndValue(rect)
            return
        old = self._shown
        self._rect = rect
        self._shown = rect
        self._dirty(old)

    def _dirty(self, old: QRectF) -> None:
        region = old.united(self._shown) if not old.isNull() else self._shown
        if not region.isNull():
            self.update(region.adjusted(-6, -6, 6, 6).toAlignedRect())

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._shown.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(theme.color("focus"), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(self._shown.adjusted(1.5, 1.5, -1.5, -1.5), RADIUS + 2, RADIUS + 2)


class Toast(QFrame):
    def __init__(self, text: str, tone: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.tone = tone
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 13, 18, 13)
        h.setSpacing(12)
        icon = Icon({"ok": "check", "warn": "warning", "danger": "warning"}.get(tone, "info"), 22)
        icon.tone = {"ok": "ok", "warn": "warn", "danger": "danger"}.get(tone, "info")
        h.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        self.text = label(text)
        h.addWidget(self.text, 1)
        self.setMaximumWidth(520)
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(180)
        self._fade.valueChanged.connect(lambda v: effect.setOpacity(float(v)))

    def fade(self, to: float, then: Callable[[], None] | None = None) -> None:
        self._fade.stop()
        self._fade.setStartValue(self.graphicsEffect().opacity())
        self._fade.setEndValue(to)
        if then is not None:
            self._fade.finished.connect(then)
        self._fade.start()

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self.fade(0.0, lambda: discard(self))

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("raised"))
        p.drawRoundedRect(QRectF(self.rect()), RADIUS, RADIUS)


class Toasts(QWidget):
    """Short-lived messages stacked in the top-right corner, replacing the old
    status line. Problems stay up longer than confirmations."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(8)
        self.v.addStretch(1)
        self.setFixedWidth(520)

    def show_message(self, text: str, tone: str) -> None:
        toast = Toast(text, tone, self)
        self.v.addWidget(toast)
        while self.v.count() > 5:  # stretch + four toasts
            old = self.v.itemAt(1).widget()
            self.v.removeWidget(old)
            discard(old)
        toast.show()
        toast.fade(1.0)
        self.relayout()
        ms = 9000 if tone in ("warn", "danger") else 5000
        QTimer.singleShot(ms, toast, lambda: toast.fade(0.0, lambda: discard(toast)))
        toast.destroyed.connect(self._toast_gone)

    def _toast_gone(self, *_args) -> None:
        # Also runs while the whole window is being torn down, when this
        # widget is already on its way out.
        if shiboken6.isValid(self):
            QTimer.singleShot(0, self, self.relayout)

    def relayout(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        # Wrapped toast text needs height-for-width, which sizeHint ignores.
        self.v.activate()
        height = self.v.totalHeightForWidth(self.width())
        if height <= 0:
            height = self.v.sizeHint().height()
        self.setGeometry(parent.width() - self.width() - 24, 72, self.width(), height)
        self.raise_()


class TabButton(QAbstractButton):
    def __init__(self, key: str, text: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.icon_name = icon
        self.setText(text)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setProperty("ownFocus", True)
        self.alert = ""  # "", "warn" or "danger": a small dot after the name

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetricsF(theme.font(12.5, QFont.Weight.DemiBold))
        return QSize(int(fm.horizontalAdvance(self.text()) + 36 + (12 if self.alert else 0)), 40)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.update()

    def sync_lit(self) -> None:
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 2, 0, -2)
        on = self.isChecked()
        lit = shows_focus(self)
        p.setPen(Qt.PenStyle.NoPen)
        if lit:
            p.setBrush(theme.color("focus"))
            p.drawRoundedRect(r, RADIUS, RADIUS)
        elif on:
            p.setBrush(theme.color("border"))
            p.drawRoundedRect(r, RADIUS, RADIUS)
        elif self.underMouse():
            p.setBrush(theme.color("surface"))
            p.drawRoundedRect(r, RADIUS, RADIUS)
        p.setPen(theme.color("on_focus" if lit else ("text" if on else "secondary")))
        p.setFont(theme.font(12.5, QFont.Weight.DemiBold))
        text_rect = r.adjusted(18, 0, -18 - (12 if self.alert else 0), 0)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.alert:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.tone_color(self.alert, lit=lit))
            p.drawEllipse(QPointF(r.right() - 18, r.center().y()), 4, 4)


class GlyphLabel(QWidget):
    """A controller button glyph that follows the input device in use."""

    def __init__(self, key: str, router, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.router = router
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Bound methods, not lambdas: the router outlives this widget, and Qt
        # only drops connections to a receiver it can see being destroyed.
        router.modeChanged.connect(self._restyle)
        router.controllersChanged.connect(self._restyle)

    def _restyle(self, *_args) -> None:
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        w = icons.glyph_width(self.key, self.router.glyph_style, 26, theme.font(11))
        return QSize(int(w) + 2, 28)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        w = icons.glyph_width(self.key, self.router.glyph_style, 26, theme.font(11))
        rect = QRectF((self.width() - w) / 2, (self.height() - 26) / 2, w, 26)
        icons.paint_glyph(p, self.key, rect, self.router.glyph_style, theme.font(11))


ACTION_GLYPH = {
    Action.UP: "dpad", Action.DOWN: "dpad", Action.LEFT: "dpad", Action.RIGHT: "dpad",
    Action.SCROLL_UP: "scroll", Action.SCROLL_DOWN: "scroll",
    Action.ACCEPT: "accept", Action.BACK: "back", Action.AUX: "aux", Action.ALT: "alt",
    Action.PREV_TAB: "prev_tab", Action.NEXT_TAB: "next_tab", Action.MENU: "menu",
}


# Hints that mean nothing to tap: choosing, moving and switching sections are
# done on the screen itself with a mouse or a finger.
POINTER_SKIP = {Action.ACCEPT, Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT,
                Action.PREV_TAB, Action.NEXT_TAB, Action.SCROLL_UP, Action.SCROLL_DOWN}


Hint = tuple  # (actions, text) or (actions, text, keys): ``keys`` overrides the key caps shown


class HintButton(QAbstractButton):
    """One entry of the hint bar: the controller button, the keyboard key and
    what they do. Clicking it does the same as pressing the button. With a
    mouse or touch screen it drops the glyphs and becomes a plain button, the
    only on-screen way to some actions (Back, Cancel, Leave setup).

    ModSync can't tell a keyboard from a controller Steam turns into keys, so
    wherever a controller may be in use both are shown. ``keys`` names the key
    caps when they differ from the usual ones (``InputRouter.key_label``), and
    an empty list means no key does this here."""

    GAP = 6

    def __init__(self, actions: list[Action], text: str, router, parent: QWidget | None = None, *,
                 keys: list[str] | None = None) -> None:
        super().__init__(parent)
        self.actions = actions
        self.router = router
        self.keys = keys
        self.compact = False  # the bar is short of room: controller glyphs only
        self.setText(text)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.clicked.connect(lambda: router.dispatch(actions[-1]))

    def key_caps(self) -> list[str]:
        if self.keys is not None:
            return list(self.keys)
        caps: list[str] = []
        for action in self.actions:
            label = self.router.key_label(action)
            if label and label not in caps:
                caps.append(label)
        return caps

    def _parts(self) -> list[tuple[str, str, float]]:
        """("pad", glyph, width) for the controller, then a "/" and ("key",
        label, width) for the keyboard."""
        font = theme.font(11)
        parts = []
        if self.router.controller_present:
            style = self.router.pad_style
            glyphs: list[str] = []
            for action in self.actions:
                if ACTION_GLYPH[action] not in glyphs:
                    glyphs.append(ACTION_GLYPH[action])
            parts += [("pad", g, icons.glyph_width(g, style, 26, font)) for g in glyphs]
        caps = [] if parts and self.compact else self.key_caps()
        if parts and caps:
            parts.append(("sep", "/", QFontMetricsF(theme.font(11.5)).horizontalAdvance("/")))
        parts += [("key", k, icons.keycap_width(k, 26, font)) for k in caps]
        return parts

    @property
    def pointer(self) -> bool:
        return self.router.mode == "mouse"

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetricsF(theme.font(11.5, QFont.Weight.DemiBold))
        if self.pointer:
            return QSize(int(fm.horizontalAdvance(self.text()) + 40), 42)
        glyphs = sum(w + self.GAP for _, _, w in self._parts())
        return QSize(int(glyphs + fm.horizontalAdvance(self.text()) + 22), 40)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.pointer:
            r = QRectF(self.rect()).adjusted(1, 4, -1, -4)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("raised" if self.underMouse() or self.isDown() else "surface"))
            p.drawRoundedRect(r, RADIUS, RADIUS)
            p.setPen(theme.color("text"))
            p.setFont(theme.font(11.5, QFont.Weight.DemiBold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.text())
            return
        if self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("surface"))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 4, -1, -4), RADIUS, RADIUS)
        x = 8.0
        font = theme.font(11)
        for kind, name, w in self._parts():
            rect = QRectF(x, (self.height() - 26) / 2, w, 26)
            if kind == "pad":
                icons.paint_glyph(p, name, rect, self.router.pad_style, font)
            elif kind == "sep":
                p.setPen(theme.color("muted"))
                p.setFont(theme.font(11.5))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            else:
                icons.paint_keycap(p, name, rect, font)
            x += w + self.GAP
        p.setPen(theme.color("secondary"))
        p.setFont(theme.font(11.5, QFont.Weight.DemiBold))
        p.drawText(QRectF(x + 2, 0, self.width() - x, self.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class HintBar(QWidget):
    """The strip along the bottom: what each button does right now, plus the
    controller ModSync can see."""

    def __init__(self, router, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.router = router
        self.setFixedHeight(52)
        h = QHBoxLayout(self)
        h.setContentsMargins(24, 4, 24, 4)
        h.setSpacing(8)
        self.device = QWidget()
        dv = QHBoxLayout(self.device)
        dv.setContentsMargins(0, 0, 0, 0)
        dv.setSpacing(8)
        self.device_icon = Icon("gamepad", 22)
        self.device_icon.tone = "muted"
        self.device_label = label("", "muted", wrap=False)
        dv.addWidget(self.device_icon)
        dv.addWidget(self.device_label)
        h.addWidget(self.device)
        h.addStretch(1)
        self.slots = QHBoxLayout()
        self.slots.setSpacing(6)
        h.addLayout(self.slots)
        self._hints: list[Hint] = []
        router.modeChanged.connect(self._refresh_device)
        router.controllersChanged.connect(self._refresh_device)
        self._refresh_device()

    def set_hints(self, hints: list[Hint]) -> None:
        if hints == self._hints:
            return
        self._hints = hints
        self._render()

    def _render(self) -> None:
        clear_layout(self.slots)
        pointer = self.router.mode == "mouse"
        for actions, text, *keys in self._hints:
            if pointer and all(a in POINTER_SKIP for a in actions):
                continue
            button = HintButton(actions, text, self.router, keys=keys[0] if keys else None)
            if not pointer and not button._parts():
                discard(button)  # nothing on this machine does it: no controller and no key
                continue
            self.slots.addWidget(button)
        self._fit()

    def _fit(self) -> None:
        """Short of room (a narrow window, many hints), drop the device name,
        then the keyboard keys beside controller glyphs. Never clip a label."""
        buttons = [self.slots.itemAt(i).widget() for i in range(self.slots.count())]
        buttons = [b for b in buttons if isinstance(b, HintButton)]
        room = self.width() - 48 - 6 * max(0, len(buttons) - 1)

        def needed() -> int:
            return sum(b.sizeHint().width() for b in buttons)

        for b in buttons:
            b.compact = False
        self.device.setVisible(needed() + self.device.sizeHint().width() + 16 <= room)
        if needed() > room:
            for b in buttons:
                b.compact = True
        for b in buttons:
            b.updateGeometry()
            b.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    @property
    def texts(self) -> list[str]:
        return [hint[1] for hint in self._hints]

    def _refresh_device(self, *_args) -> None:
        router = self.router
        names = router.controller_names
        self._render()  # pointer buttons, or glyphs for what is connected now
        if router.mode == "mouse":
            self.device_icon.set("pointer", "muted")
            self.device_label.setText("Touch" if router.pointer == "touch" else "Mouse")
        elif names:
            self.device_icon.set("gamepad", "muted")
            self.device_label.setText(names[-1] if len(names) == 1 else f"{len(names)} controllers")
        elif router.controller_present:
            self.device_icon.set("gamepad", "muted")
            self.device_label.setText("Steam Deck" if router.family == "deck" else "Controller")
        else:
            self.device_icon.set("keyboard", "muted")
            self.device_label.setText("Keyboard")

    def paintEvent(self, _event) -> None:  # noqa: N802
        QPainter(self).fillRect(self.rect(), theme.color("bar"))
