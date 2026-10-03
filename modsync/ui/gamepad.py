"""Game controllers through SDL2's GameController API, loaded with ctypes.

SDL ships in the Flatpak's KDE runtime and on SteamOS, and its mapping
database gives every common controller the same A/B/X/Y layout. Without SDL,
or with ``MODSYNC_GAMEPAD=0``, ModSync still works with the keyboard events
Steam Input produces.

SDL's HIDAPI drivers stay off. They would open the Steam Deck's own controls
next to Steam and could switch off the trackpad mouse ("lizard mode"). ModSync
only reads evdev devices, which includes the virtual pad Steam presents to the
app it launched. Steam's ``SDL_GAMECONTROLLER_IGNORE_DEVICES`` keeps the
physical device it is already translating out of the list.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)

SDL_INIT_GAMECONTROLLER = 0x2000
SDL_CONTROLLERAXISMOTION = 0x650
SDL_CONTROLLERBUTTONDOWN = 0x651
SDL_CONTROLLERBUTTONUP = 0x652
SDL_CONTROLLERDEVICEADDED = 0x653
SDL_CONTROLLERDEVICEREMOVED = 0x654

BUTTONS = {
    0: "a", 1: "b", 2: "x", 3: "y", 4: "back", 5: "guide", 6: "start", 7: "ls", 8: "rs",
    9: "lb", 10: "rb", 11: "up", 12: "down", 13: "left", 14: "right",
}
AXIS_LEFT_X, AXIS_LEFT_Y, AXIS_RIGHT_X, AXIS_RIGHT_Y, AXIS_LT, AXIS_RT = range(6)
PLAYSTATION_TYPES = {3, 4, 7}  # SDL_CONTROLLER_TYPE_PS3, PS4, PS5

# A stick or trigger counts as pressed past PRESS and released below RELEASE,
# so a resting stick that hovers near the threshold can't chatter.
PRESS = 0.6
RELEASE = 0.35


def _load_sdl():
    if os.environ.get("MODSYNC_GAMEPAD", "1").strip() == "0":
        return None
    names = ["libSDL2-2.0.so.0", "libSDL2-2.0.so", ctypes.util.find_library("SDL2")]
    for name in filter(None, names):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


class Gamepads(QObject):
    """Polls SDL on the UI thread and reports logical buttons.

    ``button(name, pressed)`` uses SDL's names (a, b, x, y, lb, rb, start,
    back, up, down, left, right) plus ``lt``/``rt`` for the triggers. The left
    stick is folded into the d-pad names. ``scroll(value)`` is the right
    stick's vertical deflection from -1 to 1, reported while it moves."""

    button = Signal(str, bool)
    scroll = Signal(float)
    changed = Signal()  # a controller was connected or removed

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._sdl = None
        self._open: dict[int, tuple[int, str, str]] = {}  # instance id -> (handle, name, style)
        self._held: set[str] = set()
        self._stick_x = self._stick_y = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._poll)
        self._event = ctypes.create_string_buffer(64)

    # --- lifecycle -------------------------------------------------------------
    def start(self) -> bool:
        sdl = _load_sdl()
        if sdl is None:
            log.info("gamepad: SDL2 not available, keyboard and mouse only")
            return False
        try:
            sdl.SDL_SetHint.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
            for hint, value in (
                (b"SDL_JOYSTICK_HIDAPI", b"0"),
                (b"SDL_NO_SIGNAL_HANDLERS", b"1"),
                (b"SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", b"1"),
            ):
                sdl.SDL_SetHint(hint, value)
            if sdl.SDL_Init(SDL_INIT_GAMECONTROLLER) != 0:
                sdl.SDL_GetError.restype = ctypes.c_char_p
                log.warning("gamepad: SDL_Init failed: %s", sdl.SDL_GetError())
                return False
            sdl.SDL_GameControllerOpen.restype = ctypes.c_void_p
            sdl.SDL_GameControllerName.restype = ctypes.c_char_p
            sdl.SDL_GameControllerName.argtypes = [ctypes.c_void_p]
            sdl.SDL_GameControllerClose.argtypes = [ctypes.c_void_p]
            sdl.SDL_GameControllerGetJoystick.restype = ctypes.c_void_p
            sdl.SDL_GameControllerGetJoystick.argtypes = [ctypes.c_void_p]
            sdl.SDL_JoystickInstanceID.argtypes = [ctypes.c_void_p]
            sdl.SDL_JoystickGetDeviceInstanceID.argtypes = [ctypes.c_int]
            sdl.SDL_PollEvent.argtypes = [ctypes.c_void_p]
            try:
                sdl.SDL_GameControllerGetType.argtypes = [ctypes.c_void_p]
            except AttributeError:  # SDL older than 2.0.12
                pass
        except (AttributeError, OSError):
            log.warning("gamepad: unusable SDL2 library", exc_info=True)
            return False
        self._sdl = sdl
        for index in range(sdl.SDL_NumJoysticks()):
            self._add(index)
        self._timer.start()
        log.info("gamepad: SDL2 ready, %d controller(s)", len(self._open))
        return True

    def stop(self) -> None:
        self._timer.stop()
        if self._sdl is None:
            return
        for handle, _, _ in self._open.values():
            self._sdl.SDL_GameControllerClose(handle)
        self._open.clear()
        self._sdl.SDL_QuitSubSystem(SDL_INIT_GAMECONTROLLER)
        self._sdl = None

    @property
    def available(self) -> bool:
        return self._sdl is not None

    @property
    def names(self) -> list[str]:
        return [name for _, name, _ in self._open.values()]

    @property
    def style(self) -> str:
        """Glyph style of the most recently connected controller."""
        styles = [style for _, _, style in self._open.values()]
        return styles[-1] if styles else "xbox"

    # --- devices -----------------------------------------------------------------
    def _add(self, index: int) -> None:
        sdl = self._sdl
        if not sdl.SDL_IsGameController(index):
            return
        instance = sdl.SDL_JoystickGetDeviceInstanceID(index)
        if instance in self._open:
            return
        handle = sdl.SDL_GameControllerOpen(index)
        if not handle:
            return
        name = (sdl.SDL_GameControllerName(handle) or b"Controller").decode("utf-8", "replace")
        style = "xbox"
        try:
            if sdl.SDL_GameControllerGetType(handle) in PLAYSTATION_TYPES:
                style = "playstation"
        except AttributeError:
            pass
        self._open[instance] = (handle, name, style)
        log.info("gamepad: connected %s (%s)", name, style)
        self.changed.emit()

    def _remove(self, instance: int) -> None:
        entry = self._open.pop(instance, None)
        if entry is None:
            return
        self._sdl.SDL_GameControllerClose(entry[0])
        log.info("gamepad: disconnected %s", entry[1])
        for name in list(self._held):
            self._held.discard(name)
            self.button.emit(name[2:] if name.startswith("s-") else name, False)
        self._stick_x = self._stick_y = 0.0
        self.scroll.emit(0.0)
        self.changed.emit()

    # --- events --------------------------------------------------------------------
    def _set(self, name: str, pressed: bool) -> None:
        if pressed == (name in self._held):
            return
        (self._held.add if pressed else self._held.discard)(name)
        self.button.emit(name, pressed)

    def _poll(self) -> None:
        sdl = self._sdl
        if sdl is None:
            return
        ptr = ctypes.addressof(self._event)
        while sdl.SDL_PollEvent(ptr):
            kind, = struct.unpack_from("<I", self._event, 0)
            if kind in (SDL_CONTROLLERBUTTONDOWN, SDL_CONTROLLERBUTTONUP):
                button, = struct.unpack_from("<B", self._event, 12)
                name = BUTTONS.get(button)
                if name:
                    self._set(name, kind == SDL_CONTROLLERBUTTONDOWN)
            elif kind == SDL_CONTROLLERAXISMOTION:
                axis, = struct.unpack_from("<B", self._event, 12)
                value, = struct.unpack_from("<h", self._event, 16)
                self._axis(axis, max(-1.0, value / 32767))
            elif kind == SDL_CONTROLLERDEVICEADDED:
                index, = struct.unpack_from("<i", self._event, 8)
                self._add(index)
            elif kind == SDL_CONTROLLERDEVICEREMOVED:
                instance, = struct.unpack_from("<i", self._event, 8)
                self._remove(instance)

    def _axis(self, axis: int, value: float) -> None:
        if axis in (AXIS_LEFT_X, AXIS_LEFT_Y):
            if axis == AXIS_LEFT_X:
                self._stick_x = value
            else:
                self._stick_y = value
            self._stick()
        elif axis == AXIS_RIGHT_Y:
            self.scroll.emit(value if abs(value) > 0.25 else 0.0)
        elif axis in (AXIS_LT, AXIS_RT):
            name = "lt" if axis == AXIS_LT else "rt"
            self._threshold(f"{name}", value)

    def _threshold(self, name: str, value: float) -> None:
        if value > PRESS:
            self._set(name, True)
        elif value < RELEASE:
            self._set(name, False)

    def _stick(self) -> None:
        """Fold the left stick into the d-pad, one direction at a time: the
        dominant axis wins so a diagonal never moves focus twice."""
        x, y = self._stick_x, self._stick_y
        horizontal = abs(x) >= abs(y)
        wanted = None
        held = next((d for d in ("s-left", "s-right", "s-up", "s-down") if d in self._held), None)
        magnitude = max(abs(x), abs(y))
        if magnitude > PRESS or (held and magnitude > RELEASE):
            if horizontal:
                wanted = "s-left" if x < 0 else "s-right"
            else:
                wanted = "s-up" if y < 0 else "s-down"
        if held and held != wanted:
            self._held.discard(held)
            self.button.emit(held[2:], False)
        if wanted and wanted != held:
            self._held.add(wanted)
            self.button.emit(wanted[2:], True)
