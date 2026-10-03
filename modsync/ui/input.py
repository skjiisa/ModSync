"""One set of actions for every way of driving ModSync.

A controller read through SDL, a Steam Deck in its desktop configuration (the
d-pad and left stick send arrow keys, A sends Return, B Escape and Y Space), a
keyboard and a mouse all end up here. Keys and buttons become ``Action``s and
are offered to the focused widget, then to each of its parents, up to the main
window, which moves focus spatially or switches sections.

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
from PySide6.QtGui import QGuiApplication, QKeyEvent, QWindow
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


def key_action(event: QKeyEvent, focus: QWidget | None) -> Action | None:
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
    if key in (K.Key_Return, K.Key_Enter, K.Key_Select, K.Key_Space):
        return Action.ACCEPT
    if key in (K.Key_Escape, K.Key_Backspace, K.Key_Back):
        return Action.BACK
    if key == K.Key_Tab:
        return Action.NEXT_TAB if ctrl else Action.NEXT
    if key == K.Key_Backtab:
        return Action.PREV_TAB if ctrl else Action.PREV
    if key == K.Key_PageUp:
        return Action.PREV_TAB if ctrl else Action.SCROLL_UP
    if key == K.Key_PageDown:
        return Action.NEXT_TAB if ctrl else Action.SCROLL_DOWN
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
        if self.gamepads is not None:
            self.pad_style = self.gamepads.style
        self.controllersChanged.emit()

    @property
    def controller_names(self) -> list[str]:
        return self.gamepads.names if self.gamepads is not None else []

    @property
    def glyph_style(self) -> str:
        return self.pad_style if self.mode == "gamepad" else "keyboard"

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

        if not self._scroll_speed:
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

    # --- dispatch ---------------------------------------------------------------------
    def dispatch(self, action: Action) -> bool:
        """Offer ``action`` to the focused widget and its ancestors; the main
        window at the top of the chain handles whatever is left."""
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
            action = key_action(event, focus)
            if action is None:
                if event.text().strip():
                    self._set_mode("keyboard")
                return False
            self._set_mode("keyboard")
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
            if isinstance(obj, QWidget) and event.key() in self._swallow and not event.isAutoRepeat():
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
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.Wheel) and isinstance(obj, QWindow):
            self._set_mode("mouse")
        return False
