"""One dark theme, sized for a Steam Deck held at arm's length.

It borrows the Deck's own vocabulary: a matte charcoal ground, flat rows with
small corners, white legends, and the blue of the app icon as the only accent.
Focus is a lit row: the focused control fills white with dark text. Nothing
glows.

ModSync always uses this theme, whatever the desktop's color scheme. Text
colors are explicit, and tests check their contrast against every surface
they sit on.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

C = {
    "night": "#1b1d21",  # the window ground
    "dusk": "#1b1d21",
    "bar": "#23262b",  # top bar, hint bar, readout panels
    "surface": "#26292f",  # rows
    "raised": "#32363d",  # rows under the mouse
    "sunken": "#16181b",  # pressed rows, wells, fields
    "border": "#3a3e45",
    "text": "#f4f5f7",
    "secondary": "#b8bdc4",
    "muted": "#9aa1aa",
    "accent": "#4fb4ff",  # links and accent text
    "accent_hi": "#7cc6ff",
    "accent_deep": "#1a9fff",  # the app icon's blue: primary fills
    "on_accent": "#00182c",
    "focus": "#f4f5f7",  # the lit row
    "focus_pressed": "#d9dce1",
    "on_focus": "#16181b",
    "on_focus_dim": "#4a4f57",
    "ok": "#7dd88b",
    "ok_on_focus": "#1d7a33",
    "warn": "#ffc857",
    "warn_on_focus": "#8a5a00",
    "warn_bg": "#3a3020",
    "danger": "#ff8a80",
    "danger_on_focus": "#b3261e",
    "danger_bg": "#3d2424",
    "info": "#8cc8ff",
    "info_on_focus": "#0b63b8",
    "disabled": "#737a84",
}

BASE_POINT_SIZE = 12.0


def color(name: str, alpha: int | None = None) -> QColor:
    c = QColor(C[name])
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def tone_color(tone: str, *, lit: bool = False) -> QColor:
    """The color a state word or dot uses, on a plain row or a lit one."""
    name = {"ok": "ok", "warn": "warn", "danger": "danger", "info": "info", "busy": "info"}.get(tone)
    if name is None:
        return color("on_focus_dim" if lit else "secondary")
    return color(f"{name}_on_focus" if lit else name)


def font(points: float, weight: QFont.Weight = QFont.Weight.Normal, *, spacing: float = 0.0) -> QFont:
    f = QFont(QApplication.font())
    f.setPointSizeF(points)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def repolish(widget: QWidget) -> None:
    """Re-apply the style sheet after a dynamic property changed, children too."""
    for w in [widget, *widget.findChildren(QWidget)]:
        w.style().unpolish(w)
        w.style().polish(w)
    widget.update()


def role(widget: QWidget, name: str) -> None:
    """Give a label (or any widget) a visual role from the style sheet."""
    if widget.property("role") == name:
        return
    widget.setProperty("role", name)
    repolish(widget)
    widget.updateGeometry()


def apply_theme(app: QApplication) -> None:
    palette = QPalette(app.palette())
    for name, value in {
        "Window": C["night"], "WindowText": C["text"], "Base": C["sunken"],
        "AlternateBase": C["surface"], "Text": C["text"], "Button": C["surface"],
        "ButtonText": C["text"], "Highlight": C["accent_deep"],
        "HighlightedText": C["on_accent"], "PlaceholderText": C["muted"],
        "ToolTipBase": C["raised"], "ToolTipText": C["text"], "Link": C["accent"],
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, name), QColor(value))
    for group in (QPalette.ColorGroup.Disabled,):
        palette.setColor(group, QPalette.ColorRole.WindowText, QColor(C["disabled"]))
        palette.setColor(group, QPalette.ColorRole.Text, QColor(C["disabled"]))
        palette.setColor(group, QPalette.ColorRole.ButtonText, QColor(C["disabled"]))
    app.setPalette(palette)
    f = app.font()
    f.setPointSizeF(max(BASE_POINT_SIZE, f.pointSizeF()))
    app.setFont(f)
    app.setStyleSheet(STYLE % C)


STYLE = """
QMainWindow { background: %(night)s; }
QLabel { color: %(text)s; background: transparent; }
QLabel:disabled { color: %(disabled)s; }
QLabel[role="secondary"] { color: %(secondary)s; }
QLabel[role="muted"] { color: %(muted)s; }
QLabel[role="title"] { font-size: 22pt; font-weight: 700; }
QLabel[role="heading"] { font-size: 14pt; font-weight: 700; }
QLabel[role="ok"] { color: %(ok)s; font-weight: 600; }
QLabel[role="warning"], QLabel[role="danger"], QLabel[role="note"] {
    border-radius: 4px; padding: 12px 16px;
}
QLabel[role="warning"] { color: %(warn)s; background: %(warn_bg)s; }
QLabel[role="danger"] { color: %(danger)s; background: %(danger_bg)s; }
QLabel[role="note"] { color: %(secondary)s; background: %(surface)s; }
QLabel[pill] { font-weight: 600; }
QLabel[pill="ok"] { color: %(ok)s; }
QLabel[pill="warn"] { color: %(warn)s; }
QLabel[pill="danger"] { color: %(danger)s; }
QLabel[pill="off"] { color: %(secondary)s; }
QLabel[pill="info"], QLabel[pill="busy"] { color: %(info)s; }

QFrame#panel { background: %(bar)s; border: none; border-radius: 4px; }
QFrame#sheet { background: %(night)s; border: none; border-radius: 6px; }

QLabel#tileTitle { font-size: 13pt; font-weight: 600; color: %(text)s; }
QLabel#tileDesc { color: %(secondary)s; }
QLabel#tileValue { color: %(secondary)s; font-weight: 600; }
QLabel#tileValue[tone="ok"] { color: %(ok)s; }
QLabel#tileValue[tone="warn"] { color: %(warn)s; }
QLabel#tileValue[tone="danger"] { color: %(danger)s; }
QLabel#tileValue[tone="info"], QLabel#tileValue[tone="busy"] { color: %(info)s; }
QLabel#tileTitle:disabled, QLabel#tileDesc:disabled, QLabel#tileValue:disabled { color: %(disabled)s; }
QAbstractButton[tileRole="primary"] QLabel#tileTitle { color: %(on_accent)s; font-weight: 700; }
QAbstractButton[tileRole="primary"] QLabel#tileDesc,
QAbstractButton[tileRole="primary"] QLabel#tileValue { color: %(on_accent)s; }
QAbstractButton[tileRole="danger"] QLabel#tileTitle { color: %(danger)s; }
QAbstractButton[lit="true"] QLabel#tileTitle { color: %(on_focus)s; }
QAbstractButton[lit="true"] QLabel#tileDesc,
QAbstractButton[lit="true"] QLabel#tileValue { color: %(on_focus_dim)s; }
QAbstractButton[lit="true"] QLabel#tileValue[tone="ok"] { color: %(ok_on_focus)s; }
QAbstractButton[lit="true"] QLabel#tileValue[tone="warn"] { color: %(warn_on_focus)s; }
QAbstractButton[lit="true"] QLabel#tileValue[tone="danger"],
QAbstractButton[lit="true"][tileRole="danger"] QLabel#tileTitle { color: %(danger_on_focus)s; }
QAbstractButton[lit="true"] QLabel#tileValue[tone="info"],
QAbstractButton[lit="true"] QLabel#tileValue[tone="busy"] { color: %(info_on_focus)s; }
QAbstractButton[tileSize="hero"] QLabel#tileTitle { font-size: 20pt; font-weight: 700; }
QAbstractButton[tileSize="choice"] QLabel#tileTitle { font-size: 15pt; font-weight: 700; }

QLineEdit {
    color: %(text)s; background: %(sunken)s; border: 2px solid %(border)s;
    border-radius: 4px; padding: 10px 14px; font-size: 15pt;
    selection-background-color: %(accent_deep)s; selection-color: %(on_accent)s;
}
QLineEdit:focus { border-color: %(focus)s; }
QPlainTextEdit {
    color: %(secondary)s; background: %(sunken)s; border: none;
    border-radius: 4px; padding: 8px; font-family: monospace; font-size: 10pt;
    selection-background-color: %(accent_deep)s; selection-color: %(on_accent)s;
}
QPlainTextEdit:focus { border: 2px solid %(focus)s; }
QProgressBar {
    color: %(text)s; background: %(sunken)s; border: none; border-radius: 3px;
    min-height: 8px; max-height: 8px; text-align: center; font-size: 1px;
}
QProgressBar::chunk { border-radius: 3px; background: %(accent_deep)s; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 6px; margin: 4px 0; }
QScrollBar::handle:vertical { background: %(border)s; border-radius: 3px; min-height: 40px; }
QScrollBar::handle:vertical:hover { background: %(muted)s; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { height: 0; }
QToolTip { color: %(text)s; background: %(raised)s; border: none; padding: 6px; }
"""
