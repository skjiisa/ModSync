"""One dark, console-style look, sized for a Steam Deck held at arm's length.

ModSync always uses this theme, whatever the desktop's color scheme. Gaming
Mode is dark, and the backdrop is painted rather than taken from the palette.
Text colors are explicit, and tests check their contrast against every
surface they sit on.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QWidget

C = {
    "night": "#070c14",  # top of the backdrop
    "dusk": "#0d1724",  # bottom of the backdrop
    "surface": "#132030",  # panels and tiles
    "raised": "#1b2b3f",  # hovered and focused tiles
    "sunken": "#0d1622",  # pressed tiles, wells
    "border": "#29405a",
    "text": "#eef4f8",
    "secondary": "#b1c3d1",
    "muted": "#8499ab",
    "accent": "#7fe0d0",  # frost
    "accent_hi": "#b0f2e6",
    "accent_deep": "#3fb8a6",
    "on_accent": "#052520",
    "ok": "#86e3a9",
    "warn": "#f6c86b",
    "warn_bg": "#33291a",
    "danger": "#ff958c",
    "danger_bg": "#3a1d1f",
    "info": "#9cc2ff",
    "aurora_a": "#36d1b5",
    "aurora_b": "#7a6cf0",
    "disabled": "#6c7f90",
}

BASE_POINT_SIZE = 12.0


def color(name: str, alpha: int | None = None) -> QColor:
    c = QColor(C[name])
    if alpha is not None:
        c.setAlpha(alpha)
    return c


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
        "Window": C["dusk"], "WindowText": C["text"], "Base": C["sunken"],
        "AlternateBase": C["surface"], "Text": C["text"], "Button": C["surface"],
        "ButtonText": C["text"], "Highlight": C["accent"],
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
QMainWindow { background: %(dusk)s; }
QLabel { color: %(text)s; background: transparent; }
QLabel:disabled { color: %(disabled)s; }
QLabel[role="secondary"] { color: %(secondary)s; }
QLabel[role="muted"] { color: %(muted)s; }
QLabel[role="eyebrow"] {
    color: %(accent)s; font-size: 10pt; font-weight: 700; letter-spacing: 2px;
}
QLabel[role="title"] { font-size: 25pt; font-weight: 800; }
QLabel[role="hero"] { font-size: 34pt; font-weight: 800; }
QLabel[role="heading"] { font-size: 14pt; font-weight: 700; }
QLabel[role="bignum"] { font-size: 46pt; font-weight: 800; color: %(text)s; }
QLabel[role="ok"] { color: %(ok)s; font-weight: 600; }
QLabel[role="warning"], QLabel[role="danger"], QLabel[role="note"] {
    border-radius: 12px; padding: 12px 16px;
}
QLabel[role="warning"] {
    color: %(warn)s; background: %(warn_bg)s; border: 1px solid #6b5426;
}
QLabel[role="danger"] {
    color: %(danger)s; background: %(danger_bg)s; border: 1px solid #6e3236;
}
QLabel[role="note"] {
    color: %(secondary)s; background: rgba(156, 194, 255, 18); border: 1px solid #2c4560;
}
QLabel[pill] {
    border-radius: 11px; padding: 3px 11px; font-size: 10pt; font-weight: 700;
}
QLabel[pill="ok"] { color: %(ok)s; background: rgba(134, 227, 169, 30); }
QLabel[pill="warn"] { color: %(warn)s; background: rgba(246, 200, 107, 30); }
QLabel[pill="danger"] { color: %(danger)s; background: rgba(255, 149, 140, 30); }
QLabel[pill="off"] { color: %(secondary)s; background: rgba(177, 195, 209, 22); }
QLabel[pill="info"] { color: %(info)s; background: rgba(156, 194, 255, 26); }
QLabel[pill="busy"] { color: %(accent)s; background: rgba(127, 224, 208, 26); }

QFrame#panel {
    background: rgba(19, 32, 48, 222); border: 1px solid rgba(80, 120, 160, 60);
    border-radius: 20px;
}
QFrame#sheet {
    background: %(surface)s; border: 1px solid %(border)s; border-radius: 24px;
}

QLabel#tileTitle { font-size: 13pt; font-weight: 700; color: %(text)s; }
QLabel#tileDesc { color: %(secondary)s; }
QLabel#tileTitle:disabled, QLabel#tileDesc:disabled { color: %(disabled)s; }
QAbstractButton[tileRole="primary"] QLabel#tileTitle,
QAbstractButton[tileRole="primary"] QLabel#tileDesc { color: %(on_accent)s; }
QAbstractButton[tileRole="danger"] QLabel#tileTitle { color: %(danger)s; }
QAbstractButton[tileSize="hero"] QLabel#tileTitle { font-size: 21pt; font-weight: 800; }
QAbstractButton[tileSize="choice"] QLabel#tileTitle { font-size: 16pt; font-weight: 800; }
QAbstractButton[tileSize="compact"] QLabel#tileTitle { font-size: 12pt; font-weight: 600; }

QLineEdit {
    color: %(text)s; background: %(sunken)s; border: 2px solid %(border)s;
    border-radius: 12px; padding: 10px 14px; font-size: 15pt;
    selection-background-color: %(accent)s; selection-color: %(on_accent)s;
}
QLineEdit:focus { border-color: %(accent)s; }
QPlainTextEdit {
    color: %(secondary)s; background: %(sunken)s; border: 1px solid %(border)s;
    border-radius: 12px; padding: 8px; font-family: monospace; font-size: 10pt;
}
QProgressBar {
    color: %(text)s; background: %(sunken)s; border: none; border-radius: 7px;
    min-height: 14px; max-height: 14px; text-align: center; font-size: 1px;
}
QProgressBar::chunk {
    border-radius: 7px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 %(accent_deep)s, stop:1 %(accent_hi)s);
}
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 0; }
QScrollBar::handle:vertical {
    background: rgba(177, 195, 209, 60); border-radius: 4px; min-height: 40px;
}
QScrollBar::handle:vertical:hover { background: rgba(177, 195, 209, 120); }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { height: 0; }
QToolTip {
    color: %(text)s; background: %(raised)s; border: 1px solid %(border)s; padding: 6px;
}
"""
