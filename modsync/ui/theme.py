"""Shared, readable presentation for the dashboard, wizard and launch hub.

Qt's Mid palette role is a bevel/border color, not secondary text. Keep text
colors explicit and test their contrast against both the window and cards.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget

LIGHT = {
    "window": "#f1f5f7", "surface": "#ffffff", "text": "#182b38",
    "secondary": "#526572", "border": "#cbd7df", "hover": "#e8f1f3",
    "accent": "#006c63", "accent_hover": "#00564f", "on_accent": "#ffffff",
    "focus": "#007f75", "warning": "#704600", "warning_bg": "#fff3d9",
    "warning_border": "#c99939", "disabled": "#667681", "progress": "#a7d8d0",
}
DARK = {
    "window": "#141c24", "surface": "#1e2a35", "text": "#edf3f7",
    "secondary": "#b0c1ce", "border": "#425565", "hover": "#2b3d4a",
    "accent": "#8cdece", "accent_hover": "#a8ebde", "on_accent": "#102c28",
    "focus": "#8cdece", "warning": "#ffe1a2", "warning_bg": "#3c301b",
    "warning_border": "#b68b39", "disabled": "#96a7b5", "progress": "#345e59",
}


def role(widget: QWidget, name: str) -> None:
    """Assign a visual role, also refreshing widgets whose status changes."""
    if widget.property("role") == name:
        return
    widget.setProperty("role", name)
    if isinstance(widget, QLabel):
        widget.setMargin(12 if name == "warning" else 0)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.updateGeometry()
    widget.update()


def apply_theme(app: QApplication, *, dark: bool | None = None) -> None:
    if dark is None:
        scheme = app.styleHints().colorScheme()
        dark = scheme == Qt.ColorScheme.Dark or (
            scheme == Qt.ColorScheme.Unknown
            and app.palette().color(QPalette.ColorRole.Window).lightness() < 128
        )
    c = DARK if dark else LIGHT
    palette = QPalette(app.palette())
    for name, color in {
        "Window": c["window"], "WindowText": c["text"], "Base": c["surface"],
        "AlternateBase": c["hover"], "Text": c["text"], "Button": c["surface"],
        "ButtonText": c["text"], "Highlight": c["accent"],
        "HighlightedText": c["on_accent"], "PlaceholderText": c["secondary"],
        "ToolTipBase": c["surface"], "ToolTipText": c["text"],
    }.items():
        palette.setColor(getattr(QPalette.ColorRole, name), QColor(color))
    app.setPalette(palette)
    font = app.font()
    font.setPointSizeF(max(11.0, font.pointSizeF()))
    app.setFont(font)
    app.setStyleSheet("""
        QMainWindow, QDialog { background: %(window)s; }
        QLabel { color: %(text)s; background: transparent; }
        QLabel[role="secondary"] { color: %(secondary)s; }
        QLabel[role="title"] { font-size: 24pt; font-weight: 700; }
        QLabel[role="step"] { color: %(secondary)s; font-weight: 600; }
        QLabel[role="warning"] {
            color: %(warning)s; background: %(warning_bg)s;
            border: 1px solid %(warning_border)s; border-radius: 7px;
        }
        QGroupBox {
            background: %(surface)s; border: 1px solid %(border)s;
            border-radius: 10px; margin-top: 12px; padding: 22px 14px 14px;
            font-weight: 600;
        }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            left: 18px; padding: 0 6px; color: %(text)s;
        }
        QPushButton {
            color: %(text)s; background: %(surface)s;
            border: 1px solid %(border)s; border-radius: 6px;
            min-height: 24px; padding: 8px 14px;
        }
        QPushButton:hover { background: %(hover)s; border-color: %(focus)s; }
        QPushButton:pressed { background: %(hover)s; }
        QPushButton[role="primary"] {
            color: %(on_accent)s; background: %(accent)s;
            border-color: %(accent)s; font-weight: 600;
        }
        QPushButton[role="primary"]:hover { background: %(accent_hover)s; }
        QPushButton:focus, QLineEdit:focus, QListWidget:focus, QPlainTextEdit:focus {
            border: 2px solid %(focus)s;
        }
        QPushButton:disabled {
            color: %(disabled)s; background: %(window)s; border-color: %(border)s;
        }
        QLineEdit, QListWidget, QPlainTextEdit {
            color: %(text)s; background: %(surface)s;
            border: 1px solid %(border)s; border-radius: 6px; padding: 8px;
            selection-background-color: %(accent)s; selection-color: %(on_accent)s;
        }
        QListWidget::item { padding: 8px; }
        QRadioButton { color: %(text)s; spacing: 10px; padding: 8px 0; }
        QRadioButton:focus { color: %(focus)s; }
        QRadioButton::indicator {
            width: 16px; height: 16px; border-radius: 10px;
            border: 2px solid %(secondary)s; background: %(surface)s;
        }
        QRadioButton::indicator:checked {
            background: %(accent)s; border: 2px solid %(focus)s;
        }
        QScrollBar:vertical { background: %(window)s; width: 12px; margin: 0; }
        QScrollBar::handle:vertical {
            background: %(border)s; border-radius: 5px; min-height: 36px;
        }
        QScrollBar::handle:vertical:hover { background: %(secondary)s; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        QProgressBar {
            color: %(text)s; background: %(window)s; border: 1px solid %(border)s;
            border-radius: 5px; min-height: 24px; text-align: center;
        }
        QProgressBar::chunk { background: %(progress)s; border-radius: 4px; }
        QScrollArea { border: none; background: transparent; }
        QToolTip {
            color: %(text)s; background: %(surface)s;
            border: 1px solid %(border)s; padding: 6px;
        }
    """ % c)
