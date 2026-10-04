"""The building blocks every screen is made of.

Everything that can be chosen is a ``Tile``: a large button with an icon, a
title and the description a tooltip would otherwise hide (a controller can't
hover). The rest dresses the window: a painted northern-sky backdrop, the
focus halo that glides between tiles, toasts, the section tabs and the hint
bar along the bottom that shows which button does what.
"""

from __future__ import annotations

import random
import time
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
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from modsync.ui import icons, theme
from modsync.ui.input import Action

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
    """A translucent card the backdrop glows through."""

    def __init__(self, parent: QWidget | None = None, *, margins: int = 24, spacing: int = 14) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(margins, margins, margins, margins)
        self.layout_.setSpacing(spacing)

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self.layout_.addWidget(widget, stretch)
        return widget


def scroller(content: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setWidget(content)
    area.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return area


class Icon(QWidget):
    """A line icon in a soft rounded square."""

    def __init__(self, name: str, size: int = 44, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.tone = "accent"
        self.boxed = True
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
        r = QRectF(self.rect())
        on_accent = self.tone == "on_accent"
        if self.boxed:
            bg = QColor(theme.C["on_accent"]) if on_accent else theme.color(self.tone if self.tone != "accent" else "accent")
            bg.setAlpha(40 if not on_accent else 34)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(r, r.width() * 0.3, r.height() * 0.3)
        fg = theme.color("on_accent") if on_accent else theme.color(self.tone)
        if not self.isEnabled():
            fg = theme.color("disabled")
        inset = r.width() * (0.22 if self.boxed else 0.04)
        icons.paint_icon(p, self.name, r.adjusted(inset, inset, -inset, -inset), fg, weight=2.0)


# --- tiles ---------------------------------------------------------------------------


class Tile(QAbstractButton):
    """A big, focusable choice: icon, title, description, optional pill.

    ``role`` is "normal", "primary" (filled with the accent) or "danger".
    ``size`` is "normal", "compact", "choice" (a tall card) or "hero"."""

    def __init__(
        self,
        title: str,
        description: str = "",
        icon: str | None = None,
        *,
        role: str = "normal",
        size: str = "normal",
        chevron: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.setProperty("tileRole", role)
        self.setProperty("tileSize", size)
        self._glow = 0.0

        vertical = size == "choice"
        outer = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        pad = {"compact": (16, 10, 14, 10), "hero": (28, 22, 28, 22), "choice": (24, 24, 24, 24)}.get(
            size, (18, 14, 18, 14))
        outer.setContentsMargins(*pad)
        outer.setSpacing(16 if not vertical else 12)
        icon_px = {"compact": 34, "hero": 60, "choice": 56}.get(size, 44)
        # Children get their parent up front: a parentless widget made visible
        # becomes a window of its own, however briefly.
        self.icon = Icon(icon or "chevron", icon_px, self)
        self.icon.setVisible(icon is not None)
        if role == "primary":
            self.icon.tone = "on_accent"
        elif role == "danger":
            self.icon.tone = "danger"
        outer.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop if vertical else Qt.AlignmentFlag.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(3)
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

        self.badge = pill()
        self.badge.setParent(self)
        self.badge.setVisible(False)
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        outer.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        self.chevron = Icon("chevron", 22, self)
        self.chevron.boxed = False
        self.chevron.tone = "muted" if role != "primary" else "on_accent"
        self.chevron.setVisible(chevron)
        outer.addWidget(self.chevron, 0, Qt.AlignmentFlag.AlignVCenter)
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
        set_pill(self.badge, text, tone)

    def set_icon(self, name: str, tone: str | None = None) -> None:
        self.icon.set(name, tone)
        self.icon.setVisible(True)

    def set_role(self, role: str) -> None:
        if self.property("tileRole") == role:
            return
        self.setProperty("tileRole", role)
        self.icon.tone = "on_accent" if role == "primary" else ("danger" if role == "danger" else "accent")
        self.chevron.tone = "on_accent" if role == "primary" else "muted"
        theme.repolish(self)

    @property
    def description(self) -> str:
        return self.desc.text()

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
        super().changeEvent(event)

    # --- painting ---
    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        size = self.property("tileSize")
        radius = 22 if size in ("hero", "choice") else 16
        role_ = self.property("tileRole")
        hot = self.isDown()
        lit = self.hasFocus() or self.underMouse()
        if not self.isEnabled():
            p.setOpacity(0.5)
        if role_ == "primary":
            grad = QLinearGradient(r.topLeft(), r.bottomRight())
            top = theme.color("accent_hi" if lit else "accent")
            grad.setColorAt(0, top)
            grad.setColorAt(1, theme.color("accent_deep" if not hot else "accent"))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad)
            p.drawRoundedRect(r, radius, radius)
            if size == "hero":
                self._paint_shine(p, r, radius)
            return
        base = theme.color("sunken" if hot else ("raised" if lit else "surface"))
        base.setAlpha(236)
        p.setBrush(base)
        border = theme.color("border")
        if role_ == "danger":
            border = QColor(theme.C["danger"])
            border.setAlpha(110 if lit else 60)
        elif lit:
            border = theme.color("accent", 120)
        p.setPen(QPen(border, 1.2))
        p.drawRoundedRect(r, radius, radius)

    def _paint_shine(self, p: QPainter, r: QRectF, radius: float) -> None:
        """A slow sheen across the hero tile, so the main action reads as alive."""
        phase = (time.monotonic() % 6.0) / 6.0
        x = r.left() - r.width() * 0.4 + phase * r.width() * 1.8
        grad = QLinearGradient(QPointF(x - 80, r.top()), QPointF(x + 80, r.bottom()))
        grad.setColorAt(0, QColor(255, 255, 255, 0))
        grad.setColorAt(0.5, QColor(255, 255, 255, 46))
        grad.setColorAt(1, QColor(255, 255, 255, 0))
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.setClipPath(path)
        p.setBrush(grad)
        p.drawRect(r)


class HeroTile(Tile):
    """The one big action on a screen (Play, Continue, Start setup)."""

    def __init__(self, title: str, description: str = "", icon: str = "play", parent: QWidget | None = None) -> None:
        super().__init__(title, description, icon, role="primary", size="hero", parent=parent)
        self.setMinimumHeight(118)
        self._sheen = QTimer(self)
        self._sheen.setInterval(50)
        self._sheen.timeout.connect(self._shine)

    def _shine(self) -> None:
        # The sheen is only painted on the primary fill; Home demotes Play to
        # a plain tile while it recommends a repair.
        if self.property("tileRole") == "primary" and self.window().isActiveWindow():
            self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        self._sheen.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._sheen.stop()
        super().hideEvent(event)


class StatusRow(Tile):
    """One line of the readiness checklist: what, its state, and a way there."""

    TONE_ICON = {"ok": ("check", "ok"), "warn": ("warning", "warn"), "danger": ("warning", "danger"),
                 "off": ("pulse", "muted"), "busy": ("sync", "accent"), "info": ("info", "info")}

    def __init__(self, title: str, icon: str, parent: QWidget | None = None) -> None:
        super().__init__(title, "", icon, size="compact", chevron=True, parent=parent)
        self.base_icon = icon

    def set_state(self, value: str, tone: str, detail: str = "") -> None:
        self.set_badge(value, tone)
        self.set_description(detail)
        self.icon.set(self.base_icon, {"ok": "ok", "warn": "warn", "danger": "danger"}.get(tone, "accent"))


# --- big readouts ------------------------------------------------------------------------


class ProgressRing(QWidget):
    """A large circular gauge for the sync completion."""

    def __init__(self, size: int = 200, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._value = 0.0
        self._target = 0.0
        self._spin = 0.0
        self.caption = ""
        self.busy = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(600)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim)
        self._spinner = QTimer(self)
        self._spinner.setInterval(33)
        self._spinner.timeout.connect(self._tick)

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
        if busy and not self._spinner.isActive():
            self._spinner.start()
        elif not busy:
            self._spinner.stop()
        self.update()

    @property
    def value(self) -> float:
        return self._target

    def _on_anim(self, v) -> None:
        if v is None:  # emitted while start and end are being replaced
            return
        self._value = float(v)
        self.update()

    def _tick(self) -> None:
        self._spin = (self._spin + 4) % 360
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        thick = side * 0.085
        r = QRectF(thick, thick, side - 2 * thick, side - 2 * thick)
        p.setPen(QPen(theme.color("sunken"), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(r, 0, 360 * 16)
        if self._value > 0:
            grad = QConicalGradient(r.center(), 90)
            grad.setColorAt(0, theme.color("accent_hi"))
            grad.setColorAt(0.5, theme.color("accent"))
            grad.setColorAt(1, theme.color("accent_deep"))
            p.setPen(QPen(QBrush(grad), thick, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(r, 90 * 16, -int(self._value / 100 * 360 * 16))
        if self.busy:
            p.setPen(QPen(theme.color("accent_hi", 150), thick * 0.35, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(r.adjusted(-thick * 0.9, -thick * 0.9, thick * 0.9, thick * 0.9),
                      int(-self._spin * 16), 50 * 16)
        p.setPen(theme.color("text"))
        p.setFont(theme.font(side * 0.15, QFont.Weight.Black))
        p.drawText(r.adjusted(0, -side * 0.08, 0, -side * 0.08), Qt.AlignmentFlag.AlignCenter, f"{int(round(self._value))}%")
        p.setPen(theme.color("secondary"))
        p.setFont(theme.font(max(9.0, side * 0.052), QFont.Weight.DemiBold, spacing=1.2))
        p.drawText(r.adjusted(0, side * 0.2, 0, side * 0.2), Qt.AlignmentFlag.AlignCenter, self.caption.upper())


class Dot(QWidget):
    """A small status light (connected / offline)."""

    def __init__(self, tone: str = "off", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tone = tone
        self.setFixedSize(14, 14)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = theme.color({"ok": "ok", "warn": "warn", "danger": "danger"}.get(self.tone, "muted"))
        glow = QRadialGradient(QPointF(7, 7), 7)
        halo = QColor(c)
        halo.setAlpha(90)
        glow.setColorAt(0, halo)
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(QRectF(0, 0, 14, 14))
        p.setBrush(c)
        p.drawEllipse(QRectF(3.5, 3.5, 7, 7))


# --- page furniture ------------------------------------------------------------------------


class PageHeader(QWidget):
    def __init__(self, eyebrow: str, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        self.eyebrow = label(eyebrow.upper(), "eyebrow", wrap=False)
        self.title = label(title, "title")
        self.subtitle = label(subtitle, "secondary")
        v.addWidget(self.eyebrow)
        v.addWidget(self.title)
        v.addWidget(self.subtitle)
        self.subtitle.setVisible(bool(subtitle))

    def set_subtitle(self, text: str) -> None:
        self.subtitle.setText(text)
        self.subtitle.setVisible(bool(text))


# --- window chrome ---------------------------------------------------------------------------


def paint_backdrop(size: QSize) -> QPixmap:
    """A night sky over a mountain range: gradient, aurora, stars, three ridges.
    Seeded, so it's the same every time and only depends on the window size."""
    w, h = max(1, size.width()), max(1, size.height())
    pix = QPixmap(w, h)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    sky = QLinearGradient(0, 0, 0, h)
    sky.setColorAt(0, theme.color("night"))
    sky.setColorAt(0.65, QColor("#0b1522"))
    sky.setColorAt(1, theme.color("dusk"))
    p.fillRect(0, 0, w, h, sky)

    for cx, cy, rx, hue, alpha in (
        (0.18, 0.05, 0.55, "aurora_a", 52),
        (0.62, 0.0, 0.5, "aurora_b", 40),
        (0.92, 0.18, 0.35, "aurora_a", 30),
    ):
        g = QRadialGradient(QPointF(cx * w, cy * h), rx * w)
        c = theme.color(hue, alpha)
        g.setColorAt(0, c)
        g.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))
        p.fillRect(0, 0, w, h, g)

    rng = random.Random(489830)
    p.setPen(Qt.PenStyle.NoPen)
    for _ in range(int(w * h / 9000)):
        x, y = rng.random() * w, rng.random() * h * 0.6
        a = int(30 + rng.random() * 110 * (1 - y / (h * 0.6)))
        p.setBrush(QColor(220, 235, 255, a))
        s = 0.6 + rng.random() * 1.1
        p.drawEllipse(QPointF(x, y), s, s)

    for depth, (base, rough, tone, alpha) in enumerate((
        (0.70, 0.16, "#14243a", 150), (0.79, 0.12, "#101d2f", 200), (0.88, 0.08, "#0b1522", 255),
    )):
        path = QPainterPath(QPointF(0, h))
        y = base * h
        x = 0.0
        step = w / (14 + depth * 6)
        path.lineTo(0, y)
        while x < w + step:
            x += step * (0.6 + rng.random() * 0.8)
            peak = base * h - rng.random() * rough * h
            path.lineTo(x - step * 0.5, peak)
            path.lineTo(x, base * h - rng.random() * rough * h * 0.35)
        path.lineTo(w, h)
        path.closeSubpath()
        c = QColor(tone)
        c.setAlpha(alpha)
        p.setBrush(c)
        p.drawPath(path)
        if depth == 0:  # snow-lit edge on the far ridge
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(theme.color("accent", 26), 1.2))
            p.drawPath(path)
            p.setPen(Qt.PenStyle.NoPen)
    p.end()
    return pix


class Backdrop(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pix: QPixmap | None = None

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._pix = None
        super().resizeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._pix is None or self._pix.size() != self.size():
            self._pix = paint_backdrop(self.size())
        p = QPainter(self)
        p.drawPixmap(event.rect(), self._pix, event.rect())


class FocusHalo(QWidget):
    """A single outline that slides to whatever has focus.

    It is one overlay over the whole window that never takes the mouse; it
    follows its target through scrolling and layout changes on a frame timer
    while the tile fill identifies the recommended action."""

    PAD = 5

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
        if widget is not None and (widget.property("noHalo") or widget is self.parent()):
            widget = None
        self.target = widget
        rect = self._target_rect()
        if rect is None:
            self._shown = QRectF()
            self._rect = QRectF()
            self.update()
            return
        if self._shown.isNull():
            self._shown = rect
            self._rect = rect
            self.update()
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
        # half-scrolled tile is framed only where it shows.
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
            self.update(region.adjusted(-14, -14, 14, 14).toAlignedRect())

    def paintEvent(self, _event) -> None:  # noqa: N802
        if self._shown.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self._shown
        radius = 26 if r.height() > 110 else 20
        p.setPen(QPen(theme.color("accent_hi"), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r, radius, radius)


class Toast(QFrame):
    def __init__(self, text: str, tone: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.tone = tone
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        h = QHBoxLayout(self)
        h.setContentsMargins(16, 12, 18, 12)
        h.setSpacing(12)
        icon = Icon({"ok": "check", "warn": "warning", "danger": "warning"}.get(tone, "info"), 30)
        icon.tone = {"ok": "ok", "warn": "warn", "danger": "danger"}.get(tone, "accent")
        h.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        self.text = label(text)
        h.addWidget(self.text, 1)
        self.setMaximumWidth(520)
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)
        self._fade = QVariantAnimation(self)
        self._fade.setDuration(220)
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
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(theme.color(self.tone if self.tone in ("ok", "warn", "danger") else "accent", 120), 1.2))
        p.setBrush(theme.color("raised", 246))
        p.drawRoundedRect(r, 16, 16)


class Toasts(QWidget):
    """Short-lived messages stacked in the top-right corner, replacing the old
    status line. Problems stay up longer than confirmations."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(10)
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
        self.setGeometry(parent.width() - self.width() - 24, 76, self.width(), height)
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
        self.alert = ""  # "", "warn" or "danger": a small dot on the tab

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetricsF(theme.font(12.5, QFont.Weight.Bold))
        return QSize(int(fm.horizontalAdvance(self.text()) + 66), 46)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        on = self.isChecked()
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("accent", 36))
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        elif self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("raised", 160))
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        fg = theme.color("accent_hi" if on else "secondary")
        icons.paint_icon(p, self.icon_name, QRectF(r.left() + 14, r.center().y() - 10, 20, 20), fg, weight=2.1)
        p.setPen(theme.color("text") if on else fg)
        p.setFont(theme.font(12.5, QFont.Weight.Bold))
        p.drawText(r.adjusted(42, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        if self.alert:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("warn" if self.alert == "warn" else "danger"))
            p.drawEllipse(QPointF(r.left() + 34, r.top() + 10), 4.5, 4.5)


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


class HintButton(QAbstractButton):
    """One "glyph + label" entry of the hint bar; clicking it does the same as
    pressing the button, so mouse users get the shortcuts too."""

    def __init__(self, actions: list[Action], text: str, router, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.actions = actions
        self.router = router
        self.setText(text)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.clicked.connect(lambda: router.dispatch(actions[-1]))

    def _glyphs(self) -> list[tuple[str, float]]:
        style = self.router.glyph_style
        return [(ACTION_GLYPH[a], icons.glyph_width(ACTION_GLYPH[a], style, 26, theme.font(11))) for a in self.actions]

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = QFontMetricsF(theme.font(11.5, QFont.Weight.DemiBold))
        glyphs = sum(w + 6 for _, w in self._glyphs())
        return QSize(int(glyphs + fm.horizontalAdvance(self.text()) + 22), 40)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.underMouse():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.color("raised", 200))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(1, 4, -1, -4), 14, 14)
        x = 8.0
        for key, w in self._glyphs():
            icons.paint_glyph(p, key, QRectF(x, (self.height() - 26) / 2, w, 26), self.router.glyph_style, theme.font(11))
            x += w + 6
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
        self.setFixedHeight(54)
        h = QHBoxLayout(self)
        h.setContentsMargins(24, 4, 24, 6)
        h.setSpacing(8)
        self.device = QWidget()
        dv = QHBoxLayout(self.device)
        dv.setContentsMargins(0, 0, 0, 0)
        dv.setSpacing(8)
        self.device_icon = Icon("gamepad", 26)
        self.device_icon.boxed = False
        self.device_icon.tone = "muted"
        self.device_label = label("", "muted", wrap=False)
        dv.addWidget(self.device_icon)
        dv.addWidget(self.device_label)
        h.addWidget(self.device)
        h.addStretch(1)
        self.slots = QHBoxLayout()
        self.slots.setSpacing(6)
        h.addLayout(self.slots)
        self._hints: list[tuple[list[Action], str]] = []
        router.modeChanged.connect(self._refresh_device)
        router.controllersChanged.connect(self._refresh_device)
        self._refresh_device()

    def set_hints(self, hints: list[tuple[list[Action], str]]) -> None:
        if hints == self._hints:
            return
        self._hints = hints
        clear_layout(self.slots)
        for actions, text in hints:
            self.slots.addWidget(HintButton(actions, text, self.router))

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self._hints]

    def _refresh_device(self, *_args) -> None:
        names = self.router.controller_names
        mode = self.router.mode
        if names:
            self.device_icon.set("gamepad", "accent" if mode == "gamepad" else "muted")
            self.device_label.setText(names[-1] if len(names) == 1 else f"{len(names)} controllers")
        else:
            icon = {"mouse": "pointer", "keyboard": "keyboard"}.get(mode, "gamepad")
            self.device_icon.set(icon, "muted")
            self.device_label.setText({"mouse": "Mouse", "keyboard": "Keyboard"}.get(mode, "Controller"))
        for i in range(self.slots.count()):
            w = self.slots.itemAt(i).widget()
            if w is not None:
                w.updateGeometry()
                w.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0, QColor(7, 12, 20, 0))
        g.setColorAt(0.35, QColor(7, 12, 20, 200))
        g.setColorAt(1, QColor(7, 12, 20, 235))
        p.fillRect(self.rect(), g)
        p.setPen(QPen(theme.color("border", 90), 1))
        p.drawLine(QPointF(24, 0.5), QPointF(self.width() - 24, 0.5))
