"""Which SteamOS device ModSync is on, and what that means for Steam.

Some changes (the launch hook's switch, the update pin) touch files Steam
rewrites from memory when it exits, so ModSync queues them until Steam is
closed. How to get there differs per device: the Deck's Power menu has
Restart Steam, while on the Steam Frame Steam runs the whole VR session and
can't be quit, so only a reboot closes it.
"""

from __future__ import annotations

from pathlib import Path


def variant() -> str | None:
    """SteamOS's ``VARIANT_ID`` (``steamdeck``, ``vr`` for the Steam Frame), or
    None when this isn't SteamOS."""
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    fields = dict(line.split("=", 1) for line in lines if "=" in line)
    fields = {k: v.strip().strip("\"'") for k, v in fields.items()}
    return fields.get("VARIANT_ID") if fields.get("ID") == "steamos" else None


def is_steam_frame() -> bool:
    return variant() == "vr"


def steam_restart_hint() -> str:
    """How to let ModSync apply a change it queued until Steam is closed."""
    if is_steam_frame():
        return (
            "Steam can't be restarted on the Steam Frame, so reboot it with ModSync's background "
            "service turned on: the service makes the change before Steam starts."
        )
    restart = "Restart Steam (Power, then Restart Steam)" if variant() == "steamdeck" else "Restart Steam"
    return f"{restart} and ModSync, either the open app or its background service, makes the change while Steam is closed."
