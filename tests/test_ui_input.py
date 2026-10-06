"""Driving ModSync without a mouse: keys and controller buttons become the
same actions, focus moves by what's on screen, sheets keep focus to
themselves, and Steam Input sending keys for a controller SDL also reads
doesn't move anything twice."""

import os
import time
import unittest
from unittest.mock import MagicMock, patch

from modsync.state import State
from tests.ui_support import QApplication, UiTestCase

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QGridLayout, QLineEdit, QPushButton, QWidget
except ImportError:  # pragma: no cover
    pass


def key(k, text="", mods=None):
    from PySide6.QtCore import QEvent

    return QKeyEvent(QEvent.Type.KeyPress, k, mods or Qt.KeyboardModifier.NoModifier, text)


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class KeyMappingTests(UiTestCase):
    def test_deck_desktop_config_keys_map_to_actions(self):
        from modsync.ui.input import Action, key_action

        K = Qt.Key
        button = QPushButton()
        expect = {
            K.Key_Up: Action.UP, K.Key_Down: Action.DOWN, K.Key_Left: Action.LEFT, K.Key_Right: Action.RIGHT,
            K.Key_Return: Action.ACCEPT,  # A in Steam's desktop configuration
            K.Key_Space: Action.ACCEPT,  # Y
            K.Key_Escape: Action.BACK,  # B
            K.Key_Backspace: Action.BACK,
            K.Key_Q: Action.PREV_TAB, K.Key_E: Action.NEXT_TAB,
            K.Key_PageDown: Action.SCROLL_DOWN, K.Key_Tab: Action.NEXT,
        }
        for k, action in expect.items():
            with self.subTest(key=k):
                self.assertEqual(key_action(key(k), button), action)
        ctrl = Qt.KeyboardModifier.ControlModifier
        self.assertEqual(key_action(key(K.Key_Tab, mods=ctrl), button), Action.NEXT_TAB)
        self.assertIsNone(key_action(key(K.Key_A, "a"), button))

    def test_text_entry_keeps_its_editing_keys(self):
        from modsync.ui.input import Action, key_action

        K = Qt.Key
        field = QLineEdit()
        for k in (K.Key_Left, K.Key_Right, K.Key_Backspace, K.Key_Space, K.Key_Q, K.Key_E):
            with self.subTest(key=k):
                self.assertIsNone(key_action(key(k, "x"), field))
        self.assertEqual(key_action(key(K.Key_Down), field), Action.DOWN)
        self.assertEqual(key_action(key(K.Key_Return), field), Action.ACCEPT)
        self.assertEqual(key_action(key(K.Key_Escape), field), Action.BACK)
        # Keys of the on-screen keyboard let letters through to the field.
        sink = type("Sink", (), {"text_sink": True})()
        self.assertIsNone(key_action(key(K.Key_Q, "q"), sink))


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SpatialNavigationTests(UiTestCase):
    def test_moves_to_the_nearest_widget_in_each_direction_and_back(self):
        from modsync.ui import nav

        scope = QWidget()
        grid = QGridLayout(scope)
        buttons = {}
        for r in range(3):
            for c in range(3):
                b = QPushButton(f"{r}{c}")
                b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                grid.addWidget(b, r, c)
                buttons[r, c] = b
        tall = QPushButton("tall")
        grid.addWidget(tall, 0, 3, 3, 1)
        scope.resize(600, 300)
        scope.show()
        self.addCleanup(scope.close)
        self.settle()
        self.assertIs(nav.neighbour(scope, buttons[1, 1], "right"), buttons[1, 2])
        self.assertIs(nav.neighbour(scope, buttons[1, 1], "down"), buttons[2, 1])
        self.assertIsNone(nav.neighbour(scope, buttons[0, 0], "up"))
        self.assertIs(nav.neighbour(scope, buttons[2, 2], "right"), tall)
        # Back the way you came returns where you were, not the nearest.
        nav.move(scope, buttons[2, 2], "right")
        self.assertIs(nav.neighbour(scope, tall, "left"), buttons[2, 2])
        hidden = buttons[1, 2]
        hidden.hide()
        self.assertIs(nav.neighbour(scope, buttons[1, 1], "right"), tall)

    def test_home_is_navigable_with_arrows(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        self.assertIs(self.app.focusWidget(), home.play)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
        self.assertIs(self.app.focusWidget(), home.open_mo2)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Left)
        self.assertIn(self.app.focusWidget(), (home.game_row, home.skse_row, home.mo2_row, home.sync_row))
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Right)
        self.assertIs(self.app.focusWidget(), home.open_mo2)  # back where it came from
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Up)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Up)
        self.assertIs(self.app.focusWidget(), window.tabs["home"])  # up onto the current section's tab

    def test_moving_along_the_tabs_switches_sections_and_down_goes_back_in(self):
        window = self.window()
        window.tabs["home"].setFocus()
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Right)
        self.assertEqual(window._current, "sync")
        self.assertIs(self.app.focusWidget(), window.tabs["sync"])
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Right)
        self.assertEqual(window._current, "system")
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
        self.assertTrue(window.pages["system"].isAncestorOf(self.app.focusWidget()))
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_E)
        self.assertEqual(window._current, "home")
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
        self.assertEqual(window._current, "home")

    def test_sections_remember_where_focus_was(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        window.go("system")
        system = window.pages["system"]
        system.steam_tile.setFocus()
        window.go("home")
        window.go("system")
        self.assertIs(self.app.focusWidget(), system.steam_tile)


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SheetTests(UiTestCase):
    def test_sheets_keep_focus_and_b_closes_them(self):
        window = self.window()
        opener = self.app.focusWidget()
        answers = []
        sheet = window.confirm("Question", "Text", [("yes", "Yes", "", "primary", None),
                                                    ("no", "No", "", "normal", None)], answers.append)
        sheet.tiles["no"].click()  # a click answers with its own key
        self.assertEqual(answers, ["no"])
        answers.clear()
        sheet = window.confirm("Question", "Text", [("yes", "Yes", "", "primary", None),
                                                    ("no", "No", "", "normal", None)], answers.append)
        self.assertIs(self.app.focusWidget(), sheet.tiles["yes"])
        for _ in range(4):  # can't wander out to the page underneath
            QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Down)
            self.assertTrue(sheet.isAncestorOf(self.app.focusWidget()))
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_E)  # sections stay put
        self.assertEqual(window._current, "home")
        self.assertIn("Close", window.hintbar.texts)
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Escape)
        self.assertEqual(answers, [None])
        self.assertIsNone(window.top_overlay)
        self.assertIs(self.app.focusWidget(), opener)
        self.assertIn("Sections", window.hintbar.texts)

    def test_on_screen_keyboard_types_with_a_controller_and_a_keyboard(self):
        from modsync.ui.input import Action, InputRouter
        from modsync.ui.overlays import KeyboardSheet

        window = self.window()
        typed = []
        sheet = KeyboardSheet(window, "Type", "Prompt", typed.append)
        sheet.open()
        self.assertIs(self.app.focusWidget(), sheet.field)  # keyboard in use: type straight away
        QTest.keyClicks(sheet.field, "ab")
        sheet.keys[0].setFocus()  # "1"
        QTest.keyClick(sheet.keys[0], Qt.Key.Key_Return)  # A presses the key
        QTest.keyClicks(sheet.keys[0], "c")  # a real keyboard still types
        router = InputRouter.instance()
        router.dispatch(Action.AUX)  # X: space
        router.dispatch(Action.ALT)  # Y: delete
        self.assertEqual(sheet.field.text(), "ab1c")
        sheet.shift.click()
        sheet.keys[10].click()  # "Q"
        with patch("modsync.ui.overlays.QGuiApplication.clipboard") as clip:
            clip.return_value.text.return_value = " -pasted "
            sheet.paste()
        router.dispatch(Action.MENU)  # Start: done
        self.assertEqual(typed, ["ab1cQ-pasted"])
        self.assertIsNone(window.top_overlay)

    def test_folder_browser_walks_the_tree(self):
        from modsync.ui.input import Action
        from modsync.ui.overlays import FolderSheet

        root = self.tmp / "tree"
        (root / "Games" / "MO2").mkdir(parents=True)
        (root / "Games" / ".hidden").mkdir()
        (root / "apps").mkdir()
        (root / "file.txt").write_text("x")
        window = self.window()
        chosen = []
        sheet = FolderSheet(window, "Pick", root, chosen.append)
        sheet.open()
        self.assertEqual([t.text() for t in sheet.entries], ["apps", "Games"])
        next(t for t in sheet.entries if t.text() == "Games").click()
        self.assertEqual([t.text() for t in sheet.entries], ["MO2"])  # dot folders stay hidden
        self.assertIs(self.app.focusWidget(), sheet.entries[0])
        sheet.handle_action(Action.ALT)  # Y: up
        self.assertEqual(sheet.path, root)
        self.assertEqual(self.app.focusWidget().text(), "Games")  # lands on the folder it left
        self.app.focusWidget().click()
        sheet.use.click()
        self.assertEqual(chosen, [root / "Games"])

    def test_folder_places_open_their_folder(self):
        from pathlib import Path
        from modsync.ui.overlays import FolderSheet

        window = self.window()
        sheet = FolderSheet(window, "Pick", self.tmp, lambda p: None)
        sheet.open()
        home = next(t for t in sheet.findChildren(type(sheet.use)) if t.text() == "Home")
        home.click()
        self.assertEqual(sheet.path, Path.home())


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GamepadRoutingTests(UiTestCase):
    def setUp(self):
        super().setUp()
        from modsync.ui.input import InputRouter

        p = patch.object(InputRouter, "_app_active", staticmethod(lambda: True))
        p.start()
        self.addCleanup(p.stop)
        self.router = InputRouter.instance()
        self.addCleanup(self._release_all)

    def _release_all(self):
        for name in list(self.router._held):
            self.router.pad_button(name, False)

    def press(self, name):
        self.router.pad_button(name, True)
        self.router.pad_button(name, False)

    def test_buttons_drive_the_window(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        self.press("down")
        self.assertIs(self.app.focusWidget(), home.open_mo2)
        self.assertEqual(self.router.mode, "gamepad")
        self.press("rb")
        self.assertEqual(window._current, "sync")
        self.press("lb")
        self.press("lb")
        self.assertEqual(window._current, "system")
        self.press("b")
        self.assertEqual(window._current, "home")
        with patch.object(window, "launch") as launch:
            window.pages["home"].play.setFocus()
            self.press("a")
        launch.assert_called_once_with(play=True)

    def test_held_direction_repeats_and_release_stops_it(self):
        window = self.window()
        window.go("system")
        first = self.app.focusWidget()
        self.router.pad_button("down", True)
        after_press = self.app.focusWidget()
        self.assertIsNot(after_press, first)
        QTest.qWait(700)  # past the repeat delay
        self.router.pad_button("down", False)
        moved_to = self.app.focusWidget()
        self.assertIsNot(moved_to, after_press)
        QTest.qWait(300)
        self.assertIs(self.app.focusWidget(), moved_to)

    def test_steam_keys_and_sdl_buttons_for_one_press_move_once(self):
        window = self.window()
        window.go("system")
        start = self.app.focusWidget()
        QTest.keyClick(start, Qt.Key.Key_Down)  # Steam Input's arrow key...
        once = self.app.focusWidget()
        self.press("down")  # ...and SDL's d-pad for the same press
        self.assertIs(self.app.focusWidget(), once)
        time.sleep(0.2)
        self.press("down")  # a real second press does move
        self.assertIsNot(self.app.focusWidget(), once)

    def test_buttons_are_ignored_while_another_app_is_in_front(self):
        from modsync.ui.input import InputRouter

        window = self.window()
        start = self.app.focusWidget()
        with patch.object(InputRouter, "_app_active", staticmethod(lambda: False)):
            self.press("down")
            self.press("a")
        self.assertIs(self.app.focusWidget(), start)
        self.assertIsNone(window.setup)

    def test_glyphs_and_halo_follow_the_device_in_use(self):
        window = self.window()
        self.assertEqual(self.router.glyph_style, "keyboard")
        self.press("down")
        self.assertEqual(self.router.glyph_style, "xbox")
        self.router.pad_style = "playstation"
        self.assertEqual(self.router.glyph_style, "playstation")
        self.router.pad_style = "xbox"
        self.assertTrue(window.halo.enabled)
        self.router._set_mode("mouse")
        self.assertFalse(window.halo.enabled)
        self.assertEqual(window.hintbar.device_label.text(), "Mouse")


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class GamepadBackendTests(UiTestCase):
    def test_left_stick_folds_into_one_direction_at_a_time(self):
        from modsync.ui.gamepad import Gamepads

        pads = Gamepads()
        events = []
        pads.button.connect(lambda name, down: events.append((name, down)))
        pads._axis(0, 0.2)  # inside the dead zone
        self.assertEqual(events, [])
        pads._axis(1, 0.9)  # down
        pads._axis(0, 0.5)  # still mostly down: no second direction
        self.assertEqual(events, [("down", True)])
        pads._axis(0, 0.95)  # now mostly right
        self.assertEqual(events[-2:], [("down", False), ("right", True)])
        pads._axis(1, 0.1)  # let go of down: still right
        self.assertEqual(events[-1], ("right", True))
        pads._axis(0, 0.1)
        self.assertEqual(events[-1], ("right", False))
        pads._axis(5, 0.9)  # right trigger
        self.assertEqual(events[-1], ("rt", True))

    def test_steams_virtual_pad_is_allowed_outside_a_steam_launch(self):
        from modsync.ui.gamepad import Gamepads

        sdl = MagicMock()
        sdl.SDL_Init.return_value = 0
        sdl.SDL_NumJoysticks.return_value = 0
        with patch("modsync.ui.gamepad._load_sdl", return_value=sdl):
            pads = Gamepads()
            self.assertTrue(pads.start())
            pads.stop()
        hints = {call.args[0]: call.args[1] for call in sdl.SDL_SetHint.call_args_list}
        self.assertEqual(hints[b"SDL_GAMECONTROLLER_ALLOW_STEAM_VIRTUAL_GAMEPAD"], b"1")
        self.assertEqual(hints[b"SDL_JOYSTICK_HIDAPI"], b"0")

    def test_no_sdl_means_keyboard_only_without_errors(self):
        from modsync.ui.gamepad import Gamepads

        with patch.dict(os.environ, {"MODSYNC_GAMEPAD": "0"}):
            pads = Gamepads()
            self.assertFalse(pads.start())
        self.assertFalse(pads.available)
        self.assertEqual(pads.names, [])
        pads.stop()


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class ReviewRegressionTests(UiTestCase):
    def setUp(self):
        super().setUp()
        from modsync.ui.input import InputRouter

        p = patch.object(InputRouter, "_app_active", staticmethod(lambda: True))
        p.start()
        self.addCleanup(p.stop)
        self.router = InputRouter.instance()

    def test_a_press_arriving_as_two_different_actions_acts_once(self):
        """Space from Steam (select on a keyboard) and Y from SDL within the
        dedupe window are one press, even though they map differently."""
        window = self.window()
        window.go("game")
        with patch.object(window.pages["game"].panel, "refresh") as refresh:
            focus = self.app.focusWidget()
            with patch.object(focus, "click") as click:
                QTest.keyClick(focus, Qt.Key.Key_Space)
                self.router.pad_button("y", True)
                self.router.pad_button("y", False)
        self.assertEqual(click.call_count + refresh.call_count, 1)

    def test_hint_bar_stays_clickable_under_a_sheet(self):
        from modsync.ui.overlays import KeyboardSheet

        window = self.window()
        typed = []
        sheet = KeyboardSheet(window, "Type", "Prompt", typed.append)
        sheet.open()
        sheet.field.setText("kept")
        self.assertLessEqual(sheet.geometry().bottom(), window.hintbar.mapTo(window.shell, window.hintbar.rect().topLeft()).y())
        done = next(b for b in window.hintbar.findChildren(type(window.hintbar.slots.itemAt(0).widget()))
                    if b.text() == "Done")
        done.click()
        self.assertEqual(typed, ["kept"])

    def test_install_keeps_focus_in_its_sheet(self):
        from threading import Event
        from modsync.mo2.installers import InstallResult

        window = self.window()
        sheet = window.start_setup().chooser.open_install()
        release = Event()
        with patch.object(sheet._installer, "available", return_value=(True, "")), \
                patch.object(sheet._installer, "install",
                             side_effect=lambda *a, **k: InstallResult(release.wait(5) and False, 1, message="stub")):
            sheet.start()
            self.settle()
            self.assertIs(self.app.focusWidget(), sheet.log)
            self.assertTrue(sheet.isAncestorOf(self.app.focusWidget()))
            release.set()
            self.settle(4)

    def test_a_rebuild_during_setup_keeps_the_tabs_hidden(self):
        window = self.window()
        window.start_setup()
        window.rebuild()
        self.assertFalse(any(tab.isVisibleTo(window) for tab in window.tabs.values()))
        window.finish_setup()
        self.assertTrue(all(tab.isVisibleTo(window) for tab in window.tabs.values()))

    def test_a_join_that_lands_after_leaving_setup_still_rebuilds(self):
        from modsync.pairing_code import PairingCode
        from modsync.service import ModSyncService
        from threading import Event

        State(instance_path=str(self.tmp)).save()
        window = self.window()
        setup = window.start_setup()
        setup.go_to(1)
        release = Event()
        with patch.object(ModSyncService, "join_vault", side_effect=lambda *a: release.wait(5)):
            setup.join.join_code(PairingCode("A" * 56, "modsync-abc", "Deck").encode())
            self.assertTrue(window.busy)  # no second join or share meanwhile
            self.assertFalse(setup.share.isEnabled())
            release.set()
            self.settle(4)
        self.assertFalse(window.busy)
        self.assertIsNone(window.setup)

    def test_a_rebuild_skipped_while_busy_happens_later_and_polling_resumes(self):
        window = self.window()
        window.prepare_rebuild()
        window.set_busy("launch", True)
        old = window.pages["home"]
        window.rebuild()
        self.assertIs(window.pages["home"], old)
        window.set_busy("launch", False)
        self.settle()
        self.assertIsNot(window.pages["home"], old)
        self.assertTrue(window._timer.isActive())

    def test_unplugging_mid_stick_releases_the_direction(self):
        from modsync.ui.gamepad import Gamepads

        pads = Gamepads()
        events = []
        pads.button.connect(lambda name, down: events.append((name, down)))
        pads._open[7] = (None, "Pad", "xbox")
        pads._sdl = type("FakeSDL", (), {"SDL_GameControllerClose": staticmethod(lambda h: None)})()
        pads._axis(0, -0.9)
        pads._remove(7)
        self.assertEqual(events, [("left", True), ("left", False)])
        pads._sdl = None


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SteamDesktopConfigTests(UiTestCase):
    """Steam's default desktop configurations, read back as the buttons that
    sent them (see modsync/ui/steaminput.py for the tables)."""

    def _family(self, family):
        from modsync.ui.input import InputRouter

        p = patch("modsync.ui.steaminput.family", return_value=family)
        p.start()
        self.addCleanup(p.stop)
        InputRouter.instance()._family = None

    def test_space_is_y_on_a_deck_b_on_a_steam_controller_and_select_on_a_keyboard(self):
        from modsync.ui.input import Action, key_action

        button = QPushButton()
        space = key(Qt.Key.Key_Space, " ")
        self.assertEqual(key_action(space, button, "deck"), Action.ALT)
        self.assertEqual(key_action(space, button, "steam"), Action.BACK)
        self.assertEqual(key_action(space, button, "keyboard"), Action.ACCEPT)
        self.assertEqual(key_action(key(Qt.Key.Key_PageUp), button, "steam"), Action.AUX)
        self.assertEqual(key_action(key(Qt.Key.Key_PageDown), button, "steam"), Action.ALT)
        self.assertEqual(key_action(key(Qt.Key.Key_PageDown), button, "deck"), Action.SCROLL_DOWN)
        for family in ("deck", "steam", "keyboard"):
            with self.subTest(family=family):
                self.assertEqual(key_action(key(Qt.Key.Key_Return), button, family), Action.ACCEPT)
                self.assertEqual(key_action(key(Qt.Key.Key_Escape), button, family), Action.BACK)
        self.assertIsNone(key_action(space, QLineEdit(), "steam"))  # typing a space still types

    def test_bumpers_arrive_as_a_tap_of_ctrl_or_alt(self):
        self._family("steam")
        window = self.window()
        focus = self.app.focusWidget()
        QTest.keyClick(focus, Qt.Key.Key_Alt, Qt.KeyboardModifier.AltModifier)  # RB
        self.assertEqual(window._current, "sync")
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)  # LB
        self.assertEqual(window._current, "home")
        from modsync.ui.input import InputRouter

        self.assertEqual(InputRouter.instance().glyph_style, "xbox")  # hints show LB/RB, not keys

    def test_a_held_modifier_used_for_a_shortcut_is_not_a_bumper(self):
        window = self.window()
        focus = self.app.focusWidget()
        QTest.keyPress(focus, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(focus, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        QTest.keyRelease(focus, Qt.Key.Key_Control)
        self.assertEqual(window._current, "home")
        QTest.keyPress(focus, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
        time.sleep(0.7)  # held, not tapped
        QTest.keyRelease(focus, Qt.Key.Key_Control)
        self.assertEqual(window._current, "home")

    def test_steam_controller_b_goes_back_instead_of_selecting(self):
        self._family("steam")
        window = self.window()
        window.go("system")
        with patch.object(self.app.focusWidget(), "click") as click:
            QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Space)  # B
        click.assert_not_called()
        self.assertEqual(window._current, "home")

    def test_deck_y_runs_the_section_shortcut(self):
        self._family("deck")
        window = self.window()
        window.go("game")
        with patch.object(window.pages["game"].panel, "refresh") as refresh, \
                patch.object(self.app.focusWidget(), "click") as click:
            QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Space)  # Y
        refresh.assert_called_once()
        click.assert_not_called()


class SteamInputFamilyTests(unittest.TestCase):
    def _sysfs(self, tmp, devices=(), dmi=None):
        from pathlib import Path

        root = Path(tmp)
        for i, (vendor, product) in enumerate(devices):
            d = root / "class" / "input" / f"input{i}" / "id"
            d.mkdir(parents=True)
            (d / "vendor").write_text(vendor + "\n")
            (d / "product").write_text(product + "\n")
        if dmi:
            d = root / "class" / "dmi" / "id"
            d.mkdir(parents=True)
            (d / "sys_vendor").write_text(dmi[0] + "\n")
            (d / "product_name").write_text(dmi[1] + "\n")
        return root

    def test_family_detection(self):
        import tempfile
        from modsync.ui import steaminput

        cases = [
            ({"devices": [("046d", "c52b")]}, False, "keyboard"),
            ({"devices": [("28de", "1304")]}, False, "steam"),  # Steam Controller puck
            ({"devices": [("28de", "1142")]}, False, "steam"),  # original Steam Controller dongle
            ({"devices": [("28de", "11ff")]}, False, "keyboard"),  # only Steam's own virtual pad
            ({"devices": [("28de", "0000")]}, False, "keyboard"),  # steamos-manager on a Frame
            ({"devices": [("045e", "028e")]}, True, "steam"),  # an Xbox pad SDL can see
            ({"devices": [("28de", "1205")]}, False, "deck"),  # the Deck's built-in controls
            ({"devices": [("28de", "1304")], "dmi": ("Valve", "Galileo")}, False, "deck"),
        ]
        for spec, pads, expected in cases:
            with self.subTest(spec=spec, pads=pads), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(steaminput.steamos, "variant", return_value=None):
                root = self._sysfs(tmp, **spec)
                self.assertEqual(steaminput.family(root, gamepads_connected=pads), expected)


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SheetDetailTests(UiTestCase):
    def test_on_screen_keyboard_has_every_symbol(self):
        import string
        from modsync.ui.overlays import KeyboardSheet

        window = self.window()
        typed = []
        sheet = KeyboardSheet(window, "Type", "Prompt", typed.append)
        sheet.open()
        sheet.symbols.click()
        self.assertEqual(sheet.symbols.text(), "abc")
        self.assertFalse(sheet.shift.isEnabled())
        reachable = {k.text() for k in sheet.keys}
        sheet.symbols.click()
        reachable |= {k.text() for k in sheet.keys}
        self.assertEqual(set(string.punctuation) - reachable, set())
        sheet.symbols.click()
        next(k for k in sheet.keys if k.text() == "@").click()
        next(k for k in sheet.keys if k.text() == "_").click()
        sheet.done()
        self.assertEqual(typed, ["@_"])

    def test_hints_name_what_a_does_on_the_focused_control(self):
        from modsync.pairing_lan import Announcement
        from modsync.ui.overlays import FolderSheet, PinSheet

        window = self.window()
        sheet = PinSheet(window, Announcement("Deck", "192.0.2.2", 21029, "s"), lambda *a: None)
        sheet.open()
        self.assertIn("Next digit", window.hintbar.texts)
        sheet.set_pin("123456")
        self.assertIn("Pair", window.hintbar.texts)
        sheet.cancel()

        root = self.tmp / "tree"
        (root / "Games").mkdir(parents=True)
        folders = FolderSheet(window, "Pick", root, lambda p: None, confirm="Use this instance")
        folders.open()
        self.assertIn("Open", window.hintbar.texts)
        folders.use.setFocus()
        self.assertIn("Use this instance", window.hintbar.texts)
        folders.cancel()

        window.request_quit()
        self.assertIn("Quit ModSync", window.hintbar.texts)


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SecondOpinionTests(UiTestCase):
    """Ideas checked against a second take on the same redesign."""

    def test_keyboard_cursor_moves_with_the_bumpers_and_clear_empties(self):
        from modsync.ui.input import Action, InputRouter
        from modsync.ui.overlays import KeyboardSheet

        window = self.window()
        sheet = KeyboardSheet(window, "Type", "Prompt", lambda text: None, text="ac")
        sheet.open()
        sheet.keys[0].setFocus()
        router = InputRouter.instance()
        router.dispatch(Action.PREV_TAB)  # LB: one to the left, between a and c
        next(k for k in sheet.keys if k.text() == "b").click()
        self.assertEqual(sheet.field.text(), "abc")
        self.assertEqual(window._current, "home")  # sections stay put under a sheet
        next(k for k in sheet.findChildren(type(sheet.keys[0])) if k.text() == "Clear").click()
        self.assertEqual(sheet.field.text(), "")

    def test_focus_recovers_when_its_control_is_disabled(self):
        from modsync.ui.widgets import TabButton

        State(instance_path=str(self.tmp)).save()
        window = self.window()
        home = window.pages["home"]
        self.assertIs(self.app.focusWidget(), home.play)
        window.set_busy("game", True)  # Play is disabled while files change
        self.settle()
        focus = self.app.focusWidget()
        self.assertIsNotNone(focus)
        self.assertNotIsInstance(focus, TabButton)
        self.assertTrue(home.isAncestorOf(focus) and focus.isEnabled())

    def test_one_press_on_a_pad_and_steams_virtual_copy_acts_once(self):
        from modsync.ui.gamepad import Gamepads

        pads = Gamepads()
        events = []
        pads.button.connect(lambda name, down: events.append((name, down)))
        # The same press, read from the physical pad and from Steam's virtual one.
        for down in (True, True, False, False):
            pads._set("a", down)
        self.assertEqual(events, [("a", True), ("a", False)])
