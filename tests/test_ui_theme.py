"""Readable text and usable action layouts in both app color schemes."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget
except ImportError:
    QApplication = None


def contrast(foreground, background):
    def luminance(hex_color):
        color = QColor(hex_color)
        values = [color.redF(), color.greenF(), color.blueF()]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in values]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    a, b = sorted((luminance(foreground), luminance(background)))
    return (b + 0.05) / (a + 0.05)


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class ThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        palette, stylesheet, font = self.app.palette(), self.app.styleSheet(), self.app.font()
        self.addCleanup(self.app.setPalette, palette)
        self.addCleanup(self.app.setFont, font)
        self.addCleanup(self.app.setStyleSheet, stylesheet)

    def test_text_has_readable_contrast_in_both_themes(self):
        from modsync.ui.theme import DARK, LIGHT, apply_theme, role
        from PySide6.QtGui import QPalette

        for dark, colors in ((False, LIGHT), (True, DARK)):
            apply_theme(self.app, dark=dark)
            label = QLabel("Setup hint")
            role(label, "secondary")
            label.ensurePolished()
            self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText).name(), colors["secondary"])
            for background in ("window", "surface"):
                for foreground in ("text", "secondary"):
                    with self.subTest(dark=dark, foreground=foreground, background=background):
                        self.assertGreaterEqual(contrast(colors[foreground], colors[background]), 4.5)
            self.assertGreaterEqual(contrast(colors["warning"], colors["warning_bg"]), 4.5)
            self.assertGreaterEqual(contrast(colors["text"], colors["progress"]), 4.5)
            self.assertGreaterEqual(contrast(colors["on_accent"], colors["accent"]), 4.5)
            self.assertGreaterEqual(contrast(colors["on_accent"], colors["accent_hover"]), 4.5)

    def test_actions_wrap_and_hidden_actions_do_not_leave_gaps(self):
        from modsync.ui.flow_layout import FlowLayout
        from modsync.ui.theme import apply_theme

        apply_theme(self.app, dark=False)
        window = QWidget()
        layout = QVBoxLayout(window)
        flow = FlowLayout()
        layout.addLayout(flow)
        layout.addStretch(1)
        buttons = [QPushButton(text) for text in (
            "Use this machine's version", "Downgrade to 1.6.1170…", "Restore original files", "Check again"
        )]
        for button in buttons:
            flow.addWidget(button)
        window.resize(420, 400)
        window.show()
        self.addCleanup(window.close)
        self.app.processEvents()
        self.assertGreater(buttons[-1].y(), buttons[0].y())
        for button in buttons:
            self.assertLessEqual(button.geometry().right(), window.width())
            self.assertGreaterEqual(button.height(), 40)
        for button in buttons[:-1]:
            button.hide()
        self.app.processEvents()
        self.assertEqual(buttons[-1].y(), layout.contentsMargins().top())
        self.assertLess(flow.heightForWidth(420), 60)
