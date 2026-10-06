"""Mouse-only and touch-screen use: first setup on a Steam Frame (whose
pointer arrives as touch) or a Deck held like a tablet, with no keyboard."""

import unittest

from modsync.state import State
from tests.ui_support import QApplication, UiTestCase

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
except ImportError:  # pragma: no cover
    pass


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class TouchTests(UiTestCase):
    def setUp(self):
        super().setUp()
        self.device = QTest.createTouchDevice()

    def drag(self, widget, start: QPoint, end: QPoint, steps: int = 10) -> None:
        QTest.touchEvent(widget, self.device).press(0, start).commit()
        for i in range(1, steps + 1):
            QTest.qWait(16)
            p = start + (end - start) * (i / steps)
            QTest.touchEvent(widget, self.device).move(0, p).commit()
        QTest.touchEvent(widget, self.device).release(0, end).commit()

    def tap(self, widget, point: QPoint) -> None:
        QTest.touchEvent(widget, self.device).press(0, point).commit()
        QTest.qWait(40)
        QTest.touchEvent(widget, self.device).release(0, point).commit()
        QTest.qWait(50)

    def test_a_finger_drags_a_section_and_taps_still_choose(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        # Touch points only reach a window inside the test platform's 800 × 800
        # screen; this is also short enough that System scrolls.
        window.resize(800, 560)
        window.go("system")
        self.settle()
        page = window.pages["system"]
        viewport = page.area.viewport()
        bar = page.area.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        # Touch points are given in window coordinates: QTest's mapping from a
        # nested widget is off by that widget's offset for touch events.
        self.drag(window, viewport.mapTo(window, QPoint(600, 400)), viewport.mapTo(window, QPoint(600, 80)))
        QTest.qWait(1200)  # momentum settles
        self.assertGreater(bar.value(), 0)
        self.assertIsNone(window.top_overlay)  # dragging over tiles chose nothing
        # Tap whichever of these tiles the drag left fully on screen.
        expected = {page.quit_tile: "Quit ModSync?", page.reset_tile: "Reset setup"}
        tile = next(t for t in expected
                    if viewport.rect().contains(t.geometry().translated(t.mapTo(viewport, QPoint(0, 0)) - t.pos())))
        self.tap(window, tile.mapTo(window, tile.rect().center()))
        self.assertEqual(window.top_overlay.title.text(), expected[tile])

    def test_dragging_from_a_tile_scrolls_without_choosing_it(self):
        from PySide6.QtWidgets import QVBoxLayout, QWidget
        from modsync.ui.widgets import Tile, scroller

        content = QWidget()
        column = QVBoxLayout(content)
        clicks = []
        tiles = []
        for i in range(20):
            tile = Tile(f"Tile {i}", "description", "box")
            tile.clicked.connect(lambda _=False, i=i: clicks.append(i))
            column.addWidget(tile)
            tiles.append(tile)
        area = scroller(content)
        area.resize(500, 400)
        area.show()
        self.addCleanup(area.close)
        self.settle()
        start = tiles[3].mapTo(area, tiles[3].rect().center())
        self.drag(area, start, start - QPoint(0, 240))  # the tile follows the finger
        QTest.qWait(1200)
        self.assertGreater(area.verticalScrollBar().value(), 0)
        self.assertEqual(clicks, [])
        visible = next(t for t in tiles if t.mapTo(area, QPoint(0, 0)).y() > 10)
        self.tap(area, visible.mapTo(area, visible.rect().center()))
        self.assertEqual(clicks, [tiles.index(visible)])

    def test_touch_shows_as_touch_and_drops_focus_styling(self):
        from modsync.ui.input import InputRouter
        from modsync.ui.widgets import shows_focus

        window = self.window()
        window.resize(800, 600)
        home = window.pages["home"]
        self.assertTrue(shows_focus(home.setup))
        self.tap(window, QPoint(400, 700))  # empty space below the tiles
        router = InputRouter.instance()
        self.assertEqual((router.mode, router.pointer), ("mouse", "touch"))
        self.assertEqual(window.hintbar.device_label.text(), "Touch")
        self.assertFalse(window.halo.enabled)
        self.assertFalse(shows_focus(home.setup))  # nothing looks selected that wasn't tapped


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class PointerTests(UiTestCase):
    def setUp(self):
        super().setUp()
        from modsync.ui.input import InputRouter

        self.router = InputRouter.instance()
        self.router._set_mode("mouse")

    def test_hint_bar_becomes_buttons_for_what_cannot_be_tapped_elsewhere(self):
        from modsync.ui.widgets import HintButton

        window = self.window()
        shown = [b.text() for b in window.hintbar.findChildren(HintButton) if b.isVisibleTo(window)]
        self.assertEqual(shown, ["Check again", "Quit"])  # no "Select" or "Sections": those are on the screen
        self.assertFalse(window.topbar.lb.isVisibleTo(window))  # no Q/E keycaps by the tabs
        next(b for b in window.hintbar.findChildren(HintButton) if b.text() == "Quit").click()
        self.assertEqual(window.top_overlay.title.text(), "Quit ModSync?")
        window.top_overlay.cancel()
        setup = window.start_setup()
        setup.go_to(1)
        self.settle()
        back = next(b for b in window.hintbar.findChildren(HintButton) if b.isVisibleTo(window))
        self.assertEqual(back.text(), "Previous step")
        back.click()
        self.assertEqual(setup.index, 0)

    def test_pin_digits_can_be_tapped(self):
        from modsync.pairing_lan import Announcement
        from modsync.ui.overlays import PinSheet

        window = self.window()
        done = []
        sheet = PinSheet(window, Announcement("Deck", "192.0.2.2", 21029, "s"), lambda a, pin: done.append(pin))
        sheet.open()
        for digit in "0428159":
            next(k for k in sheet.keypad if k.text() == digit).click()
        self.assertEqual(sheet.pin(), "042815")  # a seventh digit has nowhere to go
        sheet.delete_key.click()
        self.assertEqual(sheet.pin(), "04281")
        next(k for k in sheet.keypad if k.text() == "6").click()
        sheet.pair_button.click()
        self.assertEqual(done, ["042816"])

    def test_keyboard_sheet_starts_on_the_keys_without_a_keyboard(self):
        from modsync.ui.overlays import KeyboardSheet

        window = self.window()
        typed = []
        sheet = KeyboardSheet(window, "Type", "Prompt", typed.append)
        sheet.open()
        self.assertIs(self.app.focusWidget(), sheet.keys[0])  # not the field: no desktop keyboard pops up
        sheet.keys[11].click()  # "w"
        self.assertEqual(sheet.field.text(), "w")
        QTest.keyClicks(sheet.keys[0], "ord")  # someone picks up a real keyboard after all...
        self.assertIs(self.app.focusWidget(), sheet.field)  # ...so Enter now means Done
        QTest.keyClick(sheet.field, Qt.Key.Key_Return)
        self.assertEqual(typed, ["word"])


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class PointerDragTests(UiTestCase):
    def test_a_small_wobble_still_clicks_but_a_drag_does_not(self):
        from modsync.ui.widgets import Tile

        tile = Tile("Choose", "", "box")
        tile.resize(300, 80)
        tile.show()
        self.addCleanup(tile.close)
        clicks = []
        tile.clicked.connect(lambda: clicks.append(1))
        QTest.mousePress(tile, Qt.MouseButton.LeftButton, pos=QPoint(100, 40))
        QTest.mouseRelease(tile, Qt.MouseButton.LeftButton, pos=QPoint(106, 43))
        self.assertEqual(clicks, [1])
        QTest.mousePress(tile, Qt.MouseButton.LeftButton, pos=QPoint(100, 40))
        QTest.mouseRelease(tile, Qt.MouseButton.LeftButton, pos=QPoint(160, 40))
        self.assertEqual(clicks, [1])
        self.assertFalse(tile.isDown())
