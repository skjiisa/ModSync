"""One set of actions for every way of driving ModSync.

A controller read through SDL, a Steam Deck in its desktop configuration (the
d-pad and left stick send arrow keys, A sends Return, B Escape and Y Space), a
keyboard and a mouse all end up here. Keys and buttons become ``Action``s and
are offered to the focused widget, then to each of its parents, up to the main
window, which moves focus spatially or switches sections.

Steam's desktop configurations don't agree on what each button sends (Space
is Y on a Deck and B on everything else), so keys are read against the
controller family ``steaminput.family`` finds. Bumpers arrive as a bare tap of
Ctrl or Alt.

When Steam translates a controller into keys while SDL reads the same
controller, each press arrives twice, and not always as the same action:
Steam's desktop configuration sends Space (accept) for Y. So any press within
``DEDUPE_S`` of a press from the other source is dropped, whatever it maps to,
and so are key repeats for a direction already held on the controller.

The router also tracks which device was used last (``mode``), so the hint bar
can show the matching glyphs and the focus highlight can stay out of the way
of the mouse.
"""

from __future__ import annotations

import enum
import time

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QInputDevice, QKeyEvent, QWindow
from PySide6.QtWidgets import QApplication, QLineEdit, QPlainTextEdit, QWidget


class Action(enum.Enum):
    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    ACCEPT = "accept"
    BACK = "back"
    AUX = "aux"  # X on an Xbox pad: a page's secondary shortcut
    ALT = "alt"  # Y on an Xbox pad
    PREV_TAB = "prev_tab"
    NEXT_TAB = "next_tab"
    MENU = "menu"  # Start
    SCROLL_UP = "scroll_up"
    SCROLL_DOWN = "scroll_down"
    NEXT = "next"  # Tab
    PREV = "prev"  # Shift+Tab


DIRECTIONS = {Action.UP, Action.DOWN, Action.LEFT, Action.RIGHT}
REPEATING = DIRECTIONS | {Action.SCROLL_UP, Action.SCROLL_DOWN}

PAD = {
    "a": Action.ACCEPT, "b": Action.BACK, "x": Action.AUX, "y": Action.ALT,
    "lb": Action.PREV_TAB, "rb": Action.NEXT_TAB, "start": Action.MENU,
    "up": Action.UP, "down": Action.DOWN, "left": Action.LEFT, "right": Action.RIGHT,
    "lt": Action.SCROLL_UP, "rt": Action.SCROLL_DOWN,
}

K = Qt.Key
TEXT_KEYS = {K.Key_Left, K.Key_Right, K.Key_Backspace, K.Key_Delete, K.Key_Space, K.Key_Home, K.Key_End}

# Keys a Steam desktop configuration sends for controller buttons. When one
# arrives and a controller family is in use, the hints show controller glyphs.
CONTROLLER_KEYS = {
    K.Key_Up, K.Key_Down, K.Key_Left, K.Key_Right, K.Key_Return, K.Key_Enter,
    K.Key_Escape, K.Key_Space, K.Key_PageUp, K.Key_PageDown, K.Key_Tab,
}
TAP_KEYS = {K.Key_Control: Action.PREV_TAB, K.Key_Alt: Action.NEXT_TAB}  # LB, RB
TAP_S = 0.6  # a modifier released this soon, with nothing pressed meanwhile, is a tap
FAMILY_TTL_S = 5.0

DEDUPE_S = 0.15
REPEAT_DELAY_MS = 380
REPEAT_MS = 105
MOUSE_SLOP = 6  # pixels the pointer must travel before the mouse counts as "in use"


def is_text_input(w: QWidget | None) -> bool:
    if isinstance(w, QLineEdit):
        return not w.isReadOnly()
    if isinstance(w, QPlainTextEdit):
        return not w.isReadOnly()
    return False


def key_action(event: QKeyEvent, focus: QWidget | None, family: str = "keyboard") -> Action | None:
    """The action for a key press. ``family`` is ``steaminput.family()``:
    which button Space and Page Up/Down stand for depends on it."""
    key = event.key()
    mods = event.modifiers()
    ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
    raw = set(getattr(focus, "raw_keys", ()))
    typing = is_text_input(focus)
    if typing:
        raw |= TEXT_KEYS
    # Widgets that take typed characters (the on-screen keyboard, PIN wheels)
    # keep letter shortcuts such as Q and E for themselves.
    typing = typing or bool(getattr(focus, "text_sink", False))
    if key in raw:
        return None
    if key == K.Key_Up:
        return Action.UP
    if key == K.Key_Down:
        return Action.DOWN
    if key == K.Key_Left:
        return Action.LEFT
    if key == K.Key_Right:
        return Action.RIGHT
    if key == K.Key_Space:
        # Y on a Deck, B on other controllers, select on a keyboard.
        return {"deck": Action.ALT, "steam": Action.BACK}.get(family, Action.ACCEPT)
    if key in (K.Key_Return, K.Key_Enter, K.Key_Select):
        return Action.ACCEPT
    if key in (K.Key_Escape, K.Key_Backspace, K.Key_Back):
        return Action.BACK
    if key == K.Key_Tab:
        return Action.NEXT_TAB if ctrl else Action.NEXT
    if key == K.Key_Backtab:
        return Action.PREV_TAB if ctrl else Action.PREV
    if key == K.Key_PageUp:  # X on a Steam Controller; the R5 grip on a Deck
        return Action.PREV_TAB if ctrl else (Action.AUX if family == "steam" else Action.SCROLL_UP)
    if key == K.Key_PageDown:  # Y on a Steam Controller; the R4 grip on a Deck
        return Action.NEXT_TAB if ctrl else (Action.ALT if family == "steam" else Action.SCROLL_DOWN)
    if typing or mods & (Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.ControlModifier):
        return None
    if key in (K.Key_Q, K.Key_BracketLeft):
        return Action.PREV_TAB
    if key in (K.Key_E, K.Key_BracketRight):
        return Action.NEXT_TAB
    if key == K.Key_Home:
        return Action.MENU
    return None


class InputRouter(QObject):
    modeChanged = Signal(str)  # "mouse" | "keyboard" | "gamepad"
    controllersChanged = Signal()

    _instance: "InputRouter | None" = None

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.mode = "keyboard"
        self.pad_style = "xbox"
        self.gamepads = None
        self._swallow: set[int] = set()
        self._recent: dict[Action, tuple[str, float]] = {}
        self._last_press: tuple[str, float] | None = None  # (source, time) of the last press
        self._held: dict[str, Action] = {}  # pad button -> action, while held
        self._repeat_action: Action | None = None
        self._repeat = QTimer(self)
        self._repeat.setSingleShot(True)
        self._repeat.timeout.connect(self._on_repeat)
        self._scroll_speed = 0.0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(16)
        self._scroll_timer.timeout.connect(self._on_scroll_tick)
        self._mouse_anchor: QPoint | None = None
        self._tap: tuple[int, float] | None = None  # modifier pressed alone, and when
        self._family: tuple[str, float] | None = None
        self.last_input = 0.0  # monotonic time of the last press of any kind
        self.pointer = "mouse"  # "mouse" or "touch": which kind drove the last pointer press
        app.installEventFilter(self)

    @classmethod
    def instance(cls) -> "InputRouter":
        app = QApplication.instance()
        if cls._instance is None or cls._instance.parent() is not app:
            cls._instance = InputRouter(app)
        return cls._instance

    # --- gamepads -----------------------------------------------------------------
    def attach(self, gamepads) -> None:
        self.gamepads = gamepads
        gamepads.button.connect(self.pad_button)
        gamepads.scroll.connect(self.pad_scroll)
        gamepads.changed.connect(self._on_pads_changed)
        self._on_pads_changed()

    def _on_pads_changed(self) -> None:
        self._family = None
        if self.gamepads is not None:
            self.pad_style = self.gamepads.style
        self.controllersChanged.emit()

    @property
    def controller_names(self) -> list[str]:
        return self.gamepads.names if self.gamepads is not None else []

    @property
    def glyph_style(self) -> str:
        return self.pad_style if self.mode == "gamepad" else "keyboard"

    @property
    def family(self) -> str:
        """``steaminput.family()``, re-read every few seconds: controllers
        come and go."""
        from modsync.ui import steaminput

        now = time.monotonic()
        if self._family is None or now - self._family[1] > FAMILY_TTL_S:
            connected = bool(self.gamepads is not None and self.gamepads.names)
            self._family = (steaminput.family(gamepads_connected=connected), now)
        return self._family[0]

    def _key_mode(self, key: int) -> str:
        if key in CONTROLLER_KEYS or key in TAP_KEYS:
            return "gamepad" if self.family != "keyboard" else "keyboard"
        return "keyboard"

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.modeChanged.emit(mode)

    @staticmethod
    def _app_active() -> bool:
        return QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive

    def pad_button(self, name: str, pressed: bool) -> None:
        action = PAD.get(name)
        if action is None:
            return
        if not pressed:
            self._held.pop(name, None)
            if self._repeat_action == action and action not in self._held.values():
                self._repeat.stop()
                self._repeat_action = None
            return
        # Buttons pressed while another app is in front (MO2, the game) are
        # theirs: never click anything in ModSync behind their back.
        if not self._app_active():
            return
        self._held[name] = action
        self._set_mode("gamepad")
        if not self._duplicate(action, "pad"):
            self.dispatch(action)
        if action in REPEATING:
            self._repeat_action = action
            self._repeat.start(REPEAT_DELAY_MS)

    def pad_scroll(self, value: float) -> None:
        self._scroll_speed = value
        if value and self._app_active():
            self._set_mode("gamepad")
            if not self._scroll_timer.isActive():
                self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    def _on_scroll_tick(self) -> None:
        from modsync.ui import nav

        if not self._scroll_speed or not self._app_active():
            self._scroll_timer.stop()
            return
        area = nav.scroll_area_of(QApplication.focusWidget())
        if area is not None:
            bar = area.verticalScrollBar()
            bar.setValue(bar.value() + int(self._scroll_speed * 22))

    def _on_repeat(self) -> None:
        action = self._repeat_action
        if action is None or action not in self._held.values() or not self._app_active():
            self._repeat_action = None
            return
        self._recent[action] = ("pad", time.monotonic())
        self.dispatch(action)
        self._repeat.start(REPEAT_MS)

    def _duplicate(self, action: Action, source: str) -> bool:
        now = time.monotonic()
        self._recent[action] = (source, now)
        last, self._last_press = self._last_press, (source, now)
        return last is not None and last[0] != source and now - last[1] < DEDUPE_S

    def _tap_released(self, obj: QWidget, key: int) -> bool:
        """A bare tap of Ctrl or Alt is LB or RB: what every Steam desktop
        configuration sends for the bumpers."""
        tap, self._tap = self._tap, None
        focus = QApplication.focusWidget()
        if obj is not focus and not (focus is None and obj.isWindow()):
            return False
        if tap is None or tap[0] != key or time.monotonic() - tap[1] > TAP_S:
            return False
        if not hasattr(obj.window(), "handle_action"):
            return False
        action = TAP_KEYS[key]
        self._set_mode(self._key_mode(key))
        # SDL acts on the press; Steam's modifier acts on release. Compare
        # their press times so holding a bumper past DEDUPE_S still acts once.
        pad_press = self._recent.get(action)
        if pad_press is not None and pad_press[0] == "pad" and abs(pad_press[1] - tap[1]) < DEDUPE_S:
            return False
        if not self._duplicate(action, "key"):
            self.dispatch(action)
        return False

    # --- dispatch ---------------------------------------------------------------------
    def dispatch(self, action: Action) -> bool:
        """Offer ``action`` to the focused widget and its ancestors; the main
        window at the top of the chain handles whatever is left."""
        self.last_input = time.monotonic()
        window = QApplication.activeWindow()
        focus = QApplication.focusWidget()
        target = focus if focus is not None and (window is None or focus.window() is window) else window
        while target is not None:
            handler = getattr(target, "handle_action", None)
            if handler is not None and handler(action):
                return True
            target = target.parentWidget()
        return False

    # --- Qt events ----------------------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        kind = event.type()
        if kind == QEvent.Type.KeyPress:
            if not isinstance(obj, QWidget):
                return False
            focus = QApplication.focusWidget()
            if obj is not focus and not (focus is None and obj.isWindow()):
                return False  # an ignored key bubbling up to a parent: seen already
            if not hasattr(obj.window(), "handle_action"):
                return False  # a plain Qt dialog or another window: leave it alone
            key = event.key()
            if key in TAP_KEYS:
                # Whether the key's own modifier is already set on its press
                # differs between platforms; any other modifier means a chord.
                own = (Qt.KeyboardModifier.ControlModifier if key == K.Key_Control
                       else Qt.KeyboardModifier.AltModifier)
                others = event.modifiers() & ~own & ~Qt.KeyboardModifier.KeypadModifier
                alone = others == Qt.KeyboardModifier.NoModifier
                if not event.isAutoRepeat():
                    self._tap = (key, time.monotonic()) if alone else None
                return False
            self._tap = None
            action = key_action(event, focus, self.family)
            if action is None:
                if event.text().strip():
                    self._set_mode("keyboard")
                return False
            self._set_mode(self._key_mode(key))
            if event.key() == K.Key_Space:
                self._swallow.add(event.key())  # a button would click again on release
            if event.isAutoRepeat():
                if action not in REPEATING:
                    return True
                if action in self._held.values():
                    return True  # the controller is already repeating this
                self._recent[action] = ("key", time.monotonic())
                self.dispatch(action)
                return True
            if self._duplicate(action, "key"):
                return True
            self.dispatch(action)
            return True
        if kind == QEvent.Type.KeyRelease:
            if not isinstance(obj, QWidget) or event.isAutoRepeat():
                return False
            if event.key() in TAP_KEYS:
                return self._tap_released(obj, event.key())
            if event.key() in self._swallow:
                self._swallow.discard(event.key())
                return True
            return False
        # Pointer events reach the QWindow first, whichever widget is under it.
        if kind == QEvent.Type.MouseMove and isinstance(obj, QWindow):
            pos = event.globalPosition().toPoint()
            if self._mouse_anchor is None:
                self._mouse_anchor = pos
            elif (pos - self._mouse_anchor).manhattanLength() > MOUSE_SLOP:
                self._mouse_anchor = pos
                self._set_mode("mouse")
            return False
        if kind == QEvent.Type.TouchBegin and isinstance(obj, QWindow):
            previous, self.pointer = self.pointer, "touch"
            self.last_input = time.monotonic()
            self._tap = None
            if previous != "touch" and self.mode == "mouse":
                self.modeChanged.emit("mouse")  # relabel "Mouse" as "Touch"
            self._set_mode("mouse")
            return False
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel) and isinstance(obj, QWindow):
            touch = event.device() is not None and event.device().type() == QInputDevice.DeviceType.TouchScreen
            if kind == QEvent.Type.MouseButtonPress or not touch:
                previous, self.pointer = self.pointer, "touch" if touch else "mouse"
                if previous != self.pointer and self.mode == "mouse":
                    self.modeChanged.emit("mouse")
            self._tap = None  # Ctrl+click is not a bumper
            if kind == QEvent.Type.MouseButtonPress:
                self.last_input = time.monotonic()
            self._set_mode("mouse")
        return False
