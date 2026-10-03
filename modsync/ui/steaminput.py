"""Which controller Steam Input is likely turning into keys right now.

Steam's default desktop configurations send keys for controller buttons, and
they don't agree with each other (from Steam's own templates in
``controller_base/``):

========  =================  ======================================
Button    Steam Deck         Steam Controller, Xbox, PlayStation
========  =================  ======================================
A         Return             Enter
B         Escape             Space
X         show keyboard      Page Up
Y         Space              Page Down
LB / RB   Left Ctrl / Alt    Left Ctrl / Alt
View      Tab                Tab
Start     Escape             Escape
========  =================  ======================================

So Space means Y on a Deck and B everywhere else. ``family()`` picks the
table that applies: ``"deck"`` on Steam Deck hardware (its built-in controls
win over an external pad), ``"steam"`` when another Valve controller or a
game controller is connected, otherwise ``"keyboard"``.
"""

from __future__ import annotations

from pathlib import Path

from modsync import steamos

VALVE = "28de"
DECK_CONTROLLER = "1205"
STEAM_VIRTUAL_PAD = "11ff"  # the X-Box 360 pad Steam itself presents to games
DECK_PRODUCTS = {"Jupiter", "Galileo"}  # LCD and OLED


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def input_devices(sysfs: Path = Path("/sys")) -> list[tuple[str, str]]:
    """(vendor, product) of every input device, as lowercase hex."""
    devices = []
    try:
        entries = list((sysfs / "class" / "input").glob("input*"))
    except OSError:
        return devices
    for entry in entries:
        vendor = _read(entry / "id" / "vendor").lower()
        product = _read(entry / "id" / "product").lower()
        if vendor:
            devices.append((vendor, product))
    return devices


def is_steam_deck(sysfs: Path = Path("/sys"), devices: list[tuple[str, str]] | None = None) -> bool:
    dmi = sysfs / "class" / "dmi" / "id"
    if _read(dmi / "sys_vendor") == "Valve" and _read(dmi / "product_name") in DECK_PRODUCTS:
        return True
    if (VALVE, DECK_CONTROLLER) in (devices if devices is not None else input_devices(sysfs)):
        return True
    return steamos.variant() == "steamdeck"


def family(sysfs: Path = Path("/sys"), *, gamepads_connected: bool = False) -> str:
    devices = input_devices(sysfs)
    if is_steam_deck(sysfs, devices):
        return "deck"
    valve = any(v == VALVE and p not in (STEAM_VIRTUAL_PAD, DECK_CONTROLLER) for v, p in devices)
    if valve or gamepads_connected:
        return "steam"
    return "keyboard"
