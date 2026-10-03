"""Spatial focus navigation: the d-pad moves to whatever is next on screen.

Pages never declare a focus order. A move in some direction picks the
focusable widget whose rectangle is nearest in that direction, preferring
widgets that line up with the current one. Going back the way you came returns
to where you were, so a two-column page never loses your place.
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import QEasingCurve, QPoint, QRect, Qt, QVariantAnimation
from PySide6.QtWidgets import QAbstractScrollArea, QScrollArea, QWidget

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}


def is_focusable(w: QWidget) -> bool:
    return bool(
        w.focusPolicy() & Qt.FocusPolicy.TabFocus
        and w.isVisible()
        and w.isEnabled()
        and not w.property("navSkip")
    )


def focusables(scope: QWidget) -> list[QWidget]:
    return [w for w in scope.findChildren(QWidget) if is_focusable(w) and w.window() is scope.window()]


def rect_in(w: QWidget, scope: QWidget) -> QRect:
    return QRect(w.mapTo(scope, QPoint(0, 0)), w.size())


def reading_order(widgets: list[QWidget], scope: QWidget) -> list[QWidget]:
    return sorted(widgets, key=lambda w: (rect_in(w, scope).top() // 24, rect_in(w, scope).left()))


def _gap(a0: int, a1: int, b0: int, b1: int) -> int:
    """Distance between the ranges [a0, a1] and [b0, b1]; 0 when they overlap."""
    if b1 < a0:
        return a0 - b1
    if b0 > a1:
        return b0 - a1
    return 0


def _score(r: QRect, c: QRect, direction: str) -> float | None:
    if direction == DOWN:
        if c.center().y() <= r.center().y() or c.top() < r.top() + 4:
            return None
        primary = max(0, c.top() - r.bottom())
        ortho = _gap(r.left(), r.right(), c.left(), c.right())
        offset = abs(c.center().x() - r.center().x())
    elif direction == UP:
        if c.center().y() >= r.center().y() or c.bottom() > r.bottom() - 4:
            return None
        primary = max(0, r.top() - c.bottom())
        ortho = _gap(r.left(), r.right(), c.left(), c.right())
        offset = abs(c.center().x() - r.center().x())
    elif direction == RIGHT:
        if c.center().x() <= r.center().x() or c.left() < r.left() + 4:
            return None
        primary = max(0, c.left() - r.right())
        ortho = _gap(r.top(), r.bottom(), c.top(), c.bottom())
        offset = abs(c.center().y() - r.center().y())
    else:
        if c.center().x() >= r.center().x() or c.right() > r.right() - 4:
            return None
        primary = max(0, r.left() - c.right())
        ortho = _gap(r.top(), r.bottom(), c.top(), c.bottom())
        offset = abs(c.center().y() - r.center().y())
    return primary + ortho * 3 + offset * 0.05


def neighbour(scope: QWidget, current: QWidget | None, direction: str) -> QWidget | None:
    """The widget a move in ``direction`` from ``current`` lands on, if any."""
    candidates = focusables(scope)
    if current is None or current not in candidates:
        return None
    back = getattr(current, "_nav_back", None)
    if back is not None and back[0] == direction:
        previous = back[1]()
        if previous is not None and previous in candidates:
            return previous
    r = rect_in(current, scope)
    best, best_score = None, None
    for c in candidates:
        if c is current or current.isAncestorOf(c):
            continue
        s = _score(r, rect_in(c, scope), direction)
        if s is not None and (best_score is None or s < best_score):
            best, best_score = c, s
    return best


def move(scope: QWidget, current: QWidget | None, direction: str) -> QWidget | None:
    """Move focus; returns the newly focused widget or None at an edge."""
    target = neighbour(scope, current, direction)
    if target is None:
        return None
    target._nav_back = (OPPOSITE[direction], weakref.ref(current))  # type: ignore[attr-defined]
    target.setFocus(Qt.FocusReason.OtherFocusReason)
    return target


def step(scope: QWidget, current: QWidget | None, forward: bool) -> QWidget | None:
    """Tab / Shift+Tab: the next widget in reading order, wrapping around."""
    order = reading_order(focusables(scope), scope)
    if not order:
        return None
    if current in order:
        i = order.index(current) + (1 if forward else -1)
    else:
        i = 0 if forward else -1
    target = order[i % len(order)]
    target.setFocus(Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason)
    return target


# --- scrolling -------------------------------------------------------------------

def scroll_area_of(w: QWidget | None) -> QAbstractScrollArea | None:
    """The scrollable thing around ``w``: a page, or a log or text view."""
    while w is not None:
        if isinstance(w, QAbstractScrollArea):
            return w
        w = w.parentWidget()
    return None


def _animate(area: QAbstractScrollArea, target: int) -> None:
    bar = area.verticalScrollBar()
    target = max(bar.minimum(), min(bar.maximum(), target))
    old = getattr(area, "_scroll_anim", None)
    if old is not None:
        old.stop()
    if target == bar.value():
        return
    anim = QVariantAnimation(area)
    anim.setStartValue(bar.value())
    anim.setEndValue(target)
    anim.setDuration(170)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.valueChanged.connect(lambda v: bar.setValue(int(v)) if v is not None else None)
    area._scroll_anim = anim  # type: ignore[attr-defined]
    anim.start()


def reveal(w: QWidget, margin: int = 28) -> None:
    """Scroll ``w``'s enclosing scroll areas, smoothly, until it is in view."""
    area = scroll_area_of(w.parentWidget())
    while isinstance(area, QScrollArea):
        content = area.widget()
        if content is None or not content.isAncestorOf(w):
            break
        top = w.mapTo(content, QPoint(0, 0)).y()
        bottom = top + w.height()
        view = area.viewport().height()
        bar = area.verticalScrollBar()
        anim = getattr(area, "_scroll_anim", None)
        value = anim.endValue() if anim is not None and anim.state() == QVariantAnimation.State.Running else bar.value()
        if top - margin < value:
            _animate(area, top - margin)
        elif bottom + margin > value + view:
            _animate(area, min(top - margin, bottom + margin - view))
        w = area
        area = scroll_area_of(area.parentWidget())


def scroll_by(w: QWidget | None, pages: float) -> bool:
    """Scroll the scroll area around ``w`` by a fraction of its height."""
    area = scroll_area_of(w)
    if area is None:
        return False
    bar = area.verticalScrollBar()
    if bar.maximum() == 0:
        return False
    _animate(area, bar.value() + int(pages * area.viewport().height()))
    return True
