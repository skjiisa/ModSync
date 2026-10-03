"""Readable text in the one theme ModSync uses: every text color against
every surface it sits on, WCAG AA (4.5:1)."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication, QLabel
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

    def test_text_has_readable_contrast_on_every_surface(self):
        from PySide6.QtGui import QPalette
        from modsync.ui.theme import C, apply_theme, role

        apply_theme(self.app)
        label = QLabel("Setup hint")
        role(label, "secondary")
        label.ensurePolished()
        self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText).name(), C["secondary"])
        for background in ("night", "dusk", "surface", "raised", "sunken"):
            for foreground in ("text", "secondary", "muted", "accent", "ok", "warn", "danger", "info"):
                with self.subTest(foreground=foreground, background=background):
                    self.assertGreaterEqual(contrast(C[foreground], C[background]), 4.5)
        self.assertGreaterEqual(contrast(C["warn"], C["warn_bg"]), 4.5)
        self.assertGreaterEqual(contrast(C["danger"], C["danger_bg"]), 4.5)
        for fill in ("accent", "accent_hi", "accent_deep"):
            with self.subTest(fill=fill):
                self.assertGreaterEqual(contrast(C["on_accent"], C[fill]), 4.5)

    def test_tiles_size_to_their_text(self):
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        from modsync.ui.theme import apply_theme
        from modsync.ui.widgets import Tile

        apply_theme(self.app)
        host = QWidget()
        v = QVBoxLayout(host)
        short = Tile("Check again", "Re-read the version.", "refresh")
        long = Tile("Use this machine's version", "Record the installed runtime as the version this setup is "
                    "built for. Do this after you upgrade or downgrade the game on purpose.", "check")
        v.addWidget(short)
        v.addWidget(long)
        v.addStretch(1)
        host.resize(380, 600)
        host.show()
        self.addCleanup(host.close)
        self.app.processEvents()
        self.assertGreaterEqual(short.height(), 56)  # big enough to hit and to read across a room
        self.assertGreater(long.height(), short.height())  # descriptions wrap rather than clip
        self.assertLessEqual(long.geometry().right(), host.width())

    def test_progress_ring_animates_to_whole_percentages(self):
        from PySide6.QtTest import QTest
        from modsync.ui.widgets import ProgressRing

        ring = ProgressRing()
        ring.set_value(0, "starting", busy=True)
        ring.set_value(64, "syncing")  # an int after a float: both must interpolate
        QTest.qWait(800)
        self.assertEqual(ring._value, 64.0)
