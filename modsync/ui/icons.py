"""Hand-drawn line icons and controller button glyphs, painted with QPainter.

Nothing here depends on an icon theme or an emoji font, neither of which is
guaranteed inside the Flatpak runtime or in Gaming Mode. Icons are drawn on a
24 × 24 grid and scaled to the rect they are given.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF

from modsync.ui import theme


def _poly(*pts: float) -> QPolygonF:
    return QPolygonF([QPointF(pts[i], pts[i + 1]) for i in range(0, len(pts), 2)])


def _arc(path: QPainterPath, cx: float, cy: float, r: float, start: float, sweep: float) -> None:
    rect = QRectF(cx - r, cy - r, 2 * r, 2 * r)
    path.arcMoveTo(rect, start)
    path.arcTo(rect, start, sweep)


def _arrowhead(p: QPainter, cx: float, cy: float, r: float, angle: float, clockwise: bool) -> None:
    """A small arrowhead on a circle of radius r at ``angle`` (degrees, Qt's
    counter-clockwise convention), pointing along the direction of travel."""
    a = math.radians(angle)
    x, y = cx + r * math.cos(a), cy - r * math.sin(a)
    # Tangent of travel for a counter-clockwise sweep is (-sin, -cos) in screen space.
    tx, ty = (-math.sin(a), -math.cos(a)) if not clockwise else (math.sin(a), math.cos(a))
    nx, ny = -ty, tx
    size = 3.2
    p.drawPolyline(_poly(
        x - tx * size + nx * size, y - ty * size + ny * size,
        x, y,
        x - tx * size - nx * size, y - ty * size - ny * size,
    ))


def _draw(p: QPainter, name: str) -> None:  # noqa: C901 - a lookup table of drawings
    fill = p.pen().color()
    if name == "play":
        p.setBrush(fill)
        p.drawPolygon(_poly(8, 5, 19, 12, 8, 19))
    elif name == "play-circle":
        p.drawEllipse(QRectF(3, 3, 18, 18))
        p.setBrush(fill)
        p.drawPolygon(_poly(10, 8.5, 16, 12, 10, 15.5))
    elif name == "folder":
        path = QPainterPath(QPointF(3, 7))
        for x, y in ((3, 19), (21, 19), (21, 9), (11.5, 9), (9.5, 6), (3, 6), (3, 7)):
            path.lineTo(x, y)
        p.drawPath(path)
    elif name == "download":
        p.drawLine(QPointF(12, 4), QPointF(12, 15))
        p.drawPolyline(_poly(7, 10.5, 12, 15.5, 17, 10.5))
        p.drawLine(QPointF(5, 20), QPointF(19, 20))
    elif name == "sync":
        path = QPainterPath()
        _arc(path, 12, 12, 7.5, 30, 140)
        _arc(path, 12, 12, 7.5, 210, 140)
        p.drawPath(path)
        _arrowhead(p, 12, 12, 7.5, 170, False)
        _arrowhead(p, 12, 12, 7.5, 350, False)
    elif name == "refresh":
        path = QPainterPath()
        _arc(path, 12, 12, 7.5, 60, 280)
        p.drawPath(path)
        _arrowhead(p, 12, 12, 7.5, 340, False)
    elif name == "check":
        p.drawPolyline(_poly(5, 12.5, 10, 17.5, 19.5, 7))
    elif name == "warning":
        p.drawPolygon(_poly(12, 3.5, 21.5, 20, 2.5, 20))
        p.drawLine(QPointF(12, 9.5), QPointF(12, 14))
        p.setBrush(fill)
        p.drawEllipse(QPointF(12, 17), 0.9, 0.9)
    elif name == "gear":
        p.drawEllipse(QPointF(12, 12), 3, 3)
        p.drawEllipse(QPointF(12, 12), 6.5, 6.5)
        for i in range(8):
            a = math.radians(i * 45)
            p.drawLine(QPointF(12 + 7 * math.cos(a), 12 + 7 * math.sin(a)),
                       QPointF(12 + 9.5 * math.cos(a), 12 + 9.5 * math.sin(a)))
    elif name == "shield":
        path = QPainterPath(QPointF(12, 3))
        path.lineTo(20, 6)
        path.lineTo(20, 12)
        path.cubicTo(20, 16.5, 16.5, 19.5, 12, 21)
        path.cubicTo(7.5, 19.5, 4, 16.5, 4, 12)
        path.lineTo(4, 6)
        path.closeSubpath()
        p.drawPath(path)
        p.drawPolyline(_poly(8.5, 12, 11, 14.5, 15.5, 9.5))
    elif name == "plus-box":
        p.drawRoundedRect(QRectF(4, 4, 16, 16), 3.5, 3.5)
        p.drawLine(QPointF(12, 8), QPointF(12, 16))
        p.drawLine(QPointF(8, 12), QPointF(16, 12))
    elif name == "plus":
        p.drawLine(QPointF(12, 5), QPointF(12, 19))
        p.drawLine(QPointF(5, 12), QPointF(19, 12))
    elif name == "clipboard":
        p.drawRoundedRect(QRectF(5.5, 5, 13, 16), 2, 2)
        p.drawRoundedRect(QRectF(9, 3, 6, 4), 1.2, 1.2)
        p.drawLine(QPointF(9, 12), QPointF(15, 12))
        p.drawLine(QPointF(9, 16), QPointF(13, 16))
    elif name == "trash":
        p.drawLine(QPointF(4, 7), QPointF(20, 7))
        p.drawLine(QPointF(10, 4), QPointF(14, 4))
        p.drawPolygon(_poly(6.5, 7, 17.5, 7, 16.5, 20, 7.5, 20))
    elif name == "undo":
        p.drawPolyline(_poly(9, 5, 4, 10, 9, 15))
        path = QPainterPath(QPointF(4, 10))
        path.lineTo(14, 10)
        path.cubicTo(17.5, 10, 20, 12.5, 20, 15)
        path.cubicTo(20, 17.8, 17.5, 20, 14, 20)
        path.lineTo(10, 20)
        p.drawPath(path)
    elif name == "pin":
        p.drawEllipse(QPointF(12, 9), 4.5, 4.5)
        p.drawLine(QPointF(12, 13.5), QPointF(12, 21))
    elif name == "search":
        p.drawEllipse(QPointF(10.5, 10.5), 6, 6)
        p.drawLine(QPointF(15, 15), QPointF(20, 20))
    elif name == "radar":
        for r in (3, 6.5, 10):
            path = QPainterPath()
            _arc(path, 12, 15, r, 45, 90)
            p.drawPath(path)
        p.setBrush(fill)
        p.drawEllipse(QPointF(12, 15), 1.3, 1.3)
    elif name == "keyboard":
        p.drawRoundedRect(QRectF(2.5, 6, 19, 12), 2.5, 2.5)
        p.setBrush(fill)
        for x in (6, 9.5, 13, 16.5):
            p.drawEllipse(QPointF(x + 0.5, 10), 0.8, 0.8)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(8, 14.5), QPointF(16, 14.5))
    elif name == "qr":
        for x, y in ((3, 3), (14, 3), (3, 14)):
            p.drawRect(QRectF(x, y, 7, 7))
        p.setBrush(fill)
        for x, y in ((5.5, 5.5), (16.5, 5.5), (5.5, 16.5)):
            p.drawRect(QRectF(x, y, 2, 2))
        for x, y in ((14, 14), (18, 14), (16, 17), (14, 19), (19, 19)):
            p.drawRect(QRectF(x, y, 1.8, 1.8))
    elif name == "power":
        path = QPainterPath()
        _arc(path, 12, 13, 7.5, 125, 290)
        p.drawPath(path)
        p.drawLine(QPointF(12, 3), QPointF(12, 11))
    elif name == "wrench":
        path = QPainterPath()
        _arc(path, 15.5, 8.5, 4.5, 225, 270)
        p.drawPath(path)
        p.drawLine(QPointF(12.3, 11.7), QPointF(4.5, 19.5))
    elif name == "box":
        p.drawPolygon(_poly(12, 3, 21, 7.5, 21, 16.5, 12, 21, 3, 16.5, 3, 7.5))
        p.drawPolyline(_poly(3, 7.5, 12, 12, 21, 7.5))
        p.drawLine(QPointF(12, 12), QPointF(12, 21))
    elif name == "layers":
        p.drawPolygon(_poly(12, 4, 21, 9, 12, 14, 3, 9))
        p.drawPolyline(_poly(3, 13.5, 12, 18.5, 21, 13.5))
    elif name == "mountain":
        p.drawPolygon(_poly(2, 19.5, 9, 7.5, 13, 13.5, 16, 9.5, 22, 19.5))
        p.drawPolyline(_poly(7.2, 10.5, 9, 12, 10.8, 10.5))
    elif name == "info":
        p.drawEllipse(QPointF(12, 12), 9, 9)
        p.drawLine(QPointF(12, 11), QPointF(12, 16.5))
        p.setBrush(fill)
        p.drawEllipse(QPointF(12, 7.8), 0.9, 0.9)
    elif name == "chevron":
        p.drawPolyline(_poly(9.5, 5.5, 16, 12, 9.5, 18.5))
    elif name == "devices":
        p.drawRoundedRect(QRectF(2.5, 4.5, 14, 10), 1.5, 1.5)
        p.drawLine(QPointF(7, 18.5), QPointF(12, 18.5))
        p.drawLine(QPointF(9.5, 14.5), QPointF(9.5, 18.5))
        p.setBrush(theme.color("surface"))
        p.drawRoundedRect(QRectF(14.5, 9, 7, 11.5), 1.5, 1.5)
    elif name == "pause":
        p.drawEllipse(QPointF(12, 12), 9, 9)
        p.drawLine(QPointF(9.5, 8.5), QPointF(9.5, 15.5))
        p.drawLine(QPointF(14.5, 8.5), QPointF(14.5, 15.5))
    elif name == "stop":
        p.drawEllipse(QPointF(12, 12), 9, 9)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(9, 9, 6, 6), 1, 1)
    elif name == "up":
        p.drawPolyline(_poly(6, 11, 12, 5, 18, 11))
        p.drawLine(QPointF(12, 5), QPointF(12, 19))
    elif name == "home":
        p.drawPolyline(_poly(3, 11.5, 12, 4, 21, 11.5))
        p.drawPolyline(_poly(5.5, 9.5, 5.5, 20, 18.5, 20, 18.5, 9.5))
    elif name == "sd":
        p.drawPolygon(_poly(7, 3, 15.5, 3, 19, 6.5, 19, 21, 7, 21))
        for x in (10, 12.5, 15):
            p.drawLine(QPointF(x, 6), QPointF(x, 9))
    elif name == "globe":
        p.drawEllipse(QPointF(12, 12), 9, 9)
        p.drawEllipse(QPointF(12, 12), 4, 9)
        p.drawLine(QPointF(3, 12), QPointF(21, 12))
    elif name == "close":
        p.drawLine(QPointF(6.5, 6.5), QPointF(17.5, 17.5))
        p.drawLine(QPointF(17.5, 6.5), QPointF(6.5, 17.5))
    elif name == "moon":
        outer = QPainterPath()
        outer.addEllipse(QPointF(12, 12), 8.5, 8.5)
        bite = QPainterPath()
        bite.addEllipse(QPointF(16.5, 8.5), 7, 7)
        p.drawPath(outer.subtracted(bite))
    elif name == "pulse":
        p.drawPolyline(_poly(2.5, 12, 7, 12, 10, 5, 14, 19, 17, 12, 21.5, 12))
    elif name == "chip":
        p.drawRoundedRect(QRectF(6.5, 6.5, 11, 11), 1.5, 1.5)
        for t in (9.5, 14.5):
            for a, b, c, d in ((t, 3, t, 6.5), (t, 17.5, t, 21), (3, t, 6.5, t), (17.5, t, 21, t)):
                p.drawLine(QPointF(a, b), QPointF(c, d))
    elif name == "gamepad":
        path = QPainterPath()
        path.addRoundedRect(QRectF(2, 7, 20, 11), 5.5, 5.5)
        p.drawPath(path)
        p.drawLine(QPointF(7, 10.5), QPointF(7, 14.5))
        p.drawLine(QPointF(5, 12.5), QPointF(9, 12.5))
        p.setBrush(fill)
        p.drawEllipse(QPointF(16, 11), 1.1, 1.1)
        p.drawEllipse(QPointF(18.3, 13.6), 1.1, 1.1)
    elif name == "rocket":
        path = QPainterPath(QPointF(12, 2.5))
        path.cubicTo(16, 6, 16.5, 11, 15, 16)
        path.lineTo(9, 16)
        path.cubicTo(7.5, 11, 8, 6, 12, 2.5)
        p.drawPath(path)
        p.drawEllipse(QPointF(12, 9), 1.6, 1.6)
        p.drawPolyline(_poly(9, 13, 5.5, 16.5, 9, 17.5))
        p.drawPolyline(_poly(15, 13, 18.5, 16.5, 15, 17.5))
        p.drawLine(QPointF(12, 18.5), QPointF(12, 21.5))
    elif name == "pointer":
        p.drawPolygon(_poly(6, 3.5, 6, 19, 10, 15, 13, 21, 15.5, 20, 12.5, 14, 18, 14))
    elif name == "link":
        p.drawRoundedRect(QRectF(3, 9, 10, 6), 3, 3)
        p.drawRoundedRect(QRectF(11, 9, 10, 6), 3, 3)
    else:
        p.drawEllipse(QPointF(12, 12), 3, 3)


def paint_icon(p: QPainter, name: str, rect: QRectF, color: QColor, *, weight: float = 1.9) -> None:
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = min(rect.width(), rect.height())
    p.translate(rect.center().x() - side / 2, rect.center().y() - side / 2)
    p.scale(side / 24, side / 24)
    pen = QPen(color, weight, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    _draw(p, name)
    p.restore()


# --- controller and keyboard glyphs ---------------------------------------------

XBOX = {"accept": ("A", "#6ccf3c"), "back": ("B", "#ff6159"), "aux": ("X", "#4aa3ff"), "alt": ("Y", "#ffc53d")}
PLAYSTATION = {"accept": ("cross", "#8fb4ff"), "back": ("circle", "#ff7b7b"),
               "aux": ("square", "#f09ad8"), "alt": ("triangle", "#59d9b0")}
KEYS = {"accept": "Enter", "back": "Esc", "aux": "X", "alt": "Y", "prev_tab": "Q", "next_tab": "E",
        "menu": "Home", "scroll": "PgUp/PgDn", "dpad": "Arrows"}
SHOULDERS = {"xbox": ("LB", "RB", "☰"), "playstation": ("L1", "R1", "≡")}


def glyph_width(key: str, style: str, height: float, metrics_font: QFont) -> float:
    """How wide ``paint_glyph`` draws the glyph for ``key`` at ``height``."""
    from PySide6.QtGui import QFontMetricsF

    if style == "keyboard":
        text = KEYS.get(key, "?")
        return max(height, QFontMetricsF(metrics_font).horizontalAdvance(text) + height * 0.7)
    if key in ("prev_tab", "next_tab", "scroll"):
        return height * 1.7
    return height


def paint_glyph(p: QPainter, key: str, rect: QRectF, style: str, label_font: QFont) -> None:
    """Draw the button that triggers ``key`` (accept, back, aux, alt, prev_tab,
    next_tab, menu, dpad, scroll) in the given input style."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    text_color = theme.color("text")
    if style == "keyboard":
        p.setPen(QPen(theme.color("secondary"), 1.4))
        p.setBrush(theme.color("raised"))
        p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        p.setPen(text_color)
        f = QFont(label_font)
        f.setPointSizeF(label_font.pointSizeF() * 0.82)
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, KEYS.get(key, "?"))
        p.restore()
        return
    if key in ("prev_tab", "next_tab", "scroll"):
        left, right, _ = SHOULDERS.get(style, SHOULDERS["xbox"])
        text = {"prev_tab": left, "next_tab": right, "scroll": "LT/RT" if style != "playstation" else "L2/R2"}[key]
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("raised"))
        p.drawRoundedRect(rect, rect.height() / 2.6, rect.height() / 2.6)
        p.setPen(text_color)
        f = QFont(label_font)
        f.setPointSizeF(label_font.pointSizeF() * 0.78)
        f.setBold(True)
        p.setFont(f)
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()
        return
    if key == "dpad":
        c = rect.center()
        s = rect.height() / 2
        arm = s * 0.36
        path = QPainterPath()
        path.addRoundedRect(QRectF(c.x() - arm, c.y() - s, arm * 2, s * 2), 2, 2)
        path.addRoundedRect(QRectF(c.x() - s, c.y() - arm, s * 2, arm * 2), 2, 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("secondary"))
        p.drawPath(path.simplified())
        p.restore()
        return
    if key == "menu":
        _, _, sym = SHOULDERS.get(style, SHOULDERS["xbox"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.color("raised"))
        p.drawEllipse(rect)
        pen = QPen(text_color, max(1.4, rect.height() / 12), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        for dy in (-0.2, 0, 0.2):
            y = rect.center().y() + dy * rect.height()
            p.drawLine(QPointF(rect.left() + rect.width() * 0.3, y), QPointF(rect.right() - rect.width() * 0.3, y))
        p.restore()
        return

    table = PLAYSTATION if style == "playstation" else XBOX
    label, hue = table.get(key, ("?", "#888888"))
    accent = QColor(hue)
    p.setPen(QPen(accent, 1.6))
    p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 46)))
    inner = rect.adjusted(0.8, 0.8, -0.8, -0.8)
    p.drawEllipse(inner)
    if style == "playstation":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(accent, max(1.5, rect.height() / 13), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        c = inner.center()
        r = inner.height() * 0.24
        if label == "cross":
            p.drawLine(QPointF(c.x() - r, c.y() - r), QPointF(c.x() + r, c.y() + r))
            p.drawLine(QPointF(c.x() + r, c.y() - r), QPointF(c.x() - r, c.y() + r))
        elif label == "circle":
            p.drawEllipse(c, r * 1.05, r * 1.05)
        elif label == "square":
            p.drawRect(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r))
        else:
            p.drawPolygon(_poly(c.x(), c.y() - r * 1.15, c.x() + r * 1.15, c.y() + r * 0.8,
                                c.x() - r * 1.15, c.y() + r * 0.8))
    else:
        p.setPen(accent)
        f = QFont(label_font)
        f.setPointSizeF(label_font.pointSizeF() * 0.86)
        f.setBold(True)
        p.setFont(f)
        p.drawText(inner, Qt.AlignmentFlag.AlignCenter, label)
    p.restore()
