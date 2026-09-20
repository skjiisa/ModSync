"""Add (or remove) ModSync as a non-Steam game by editing Steam's ``shortcuts.vdf``.

This is what makes ModSync reachable from **Gaming Mode** on the Steam Deck (where
there's no desktop launcher and text fields get the on-screen keyboard).

Unlike ``libraryfolders.vdf``/``*.acf`` (text), ``shortcuts.vdf`` is Valve's
*binary* KeyValues. We implement the small subset it uses: nested maps (``0x00``),
strings (``0x01``) and int32s (``0x02``), each map terminated by ``0x08``. The
codec round-trips Steam's own files byte-for-byte.
"""

from __future__ import annotations

import binascii
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from modsync import platforms

MODSYNC_FLATPAK_ID = "io.github.skjiisa.ModSync"

# binary KeyValues type tags
_MAP = 0x00
_STR = 0x01
_INT = 0x02
_END = 0x08


class VdfError(ValueError):
    pass


# --- binary KeyValues codec ---------------------------------------------------

def _read_cstr(data: bytes, i: int) -> tuple[str, int]:
    end = data.index(0x00, i)
    return data[i:end].decode("utf-8", "replace"), end + 1


def _read_map(data: bytes, i: int) -> tuple[dict, int]:
    obj: dict[str, object] = {}
    while True:
        if i >= len(data):
            raise VdfError("unexpected end of shortcuts.vdf")
        tag = data[i]
        i += 1
        if tag == _END:
            return obj, i
        key, i = _read_cstr(data, i)
        if tag == _MAP:
            obj[key], i = _read_map(data, i)
        elif tag == _STR:
            obj[key], i = _read_cstr(data, i)
        elif tag == _INT:
            obj[key] = struct.unpack_from("<i", data, i)[0]
            i += 4
        else:
            raise VdfError(f"unsupported KeyValues tag {tag:#x}")


def loads(data: bytes) -> dict:
    obj, _ = _read_map(data, 0)
    return obj


def load(path: Path | str) -> dict:
    return loads(Path(path).read_bytes())


def _write_map(obj: dict, out: bytearray) -> None:
    for key, val in obj.items():
        kb = key.encode("utf-8") + b"\x00"
        if isinstance(val, dict):
            out += bytes([_MAP]) + kb
            _write_map(val, out)
        elif isinstance(val, bool):  # bool subclasses int — handle before int
            out += bytes([_INT]) + kb + struct.pack("<i", int(val))
        elif isinstance(val, int):
            out += bytes([_INT]) + kb + struct.pack("<i", val)
        elif isinstance(val, str):
            out += bytes([_STR]) + kb + val.encode("utf-8") + b"\x00"
        else:
            raise VdfError(f"cannot serialise {type(val).__name__} for key {key!r}")
    out += bytes([_END])


def dumps(obj: dict) -> bytes:
    out = bytearray()
    _write_map(obj, out)
    return bytes(out)


def dump(path: Path | str, obj: dict) -> None:
    Path(path).write_bytes(dumps(obj))


# --- shortcut entries ---------------------------------------------------------

def _ci_get(entry: dict, key: str) -> object:
    kl = key.lower()
    for k, v in entry.items():
        if k.lower() == kl:
            return v
    return None


def _shortcut_appid(exe: str, app_name: str) -> int:
    """Steam's legacy non-Steam-shortcut id: crc32(exe+appname) with the high bit
    set, as a signed int32 (so grid art / the appid field stay stable)."""
    crc = binascii.crc32((exe + app_name).encode("utf-8")) & 0xFFFFFFFF
    return (crc | 0x80000000) - 0x100000000


def add_or_update(
    root: dict,
    *,
    app_name: str,
    exe: str,
    start_dir: str,
    launch_options: str = "",
    icon: str = "",
    tags: list[str] | None = None,
) -> int:
    """Add (or update, by AppName) a shortcut under ``root['shortcuts']``.

    Idempotent: re-running with the same AppName updates the existing entry
    rather than creating a duplicate. Returns the entry's integer index.
    """
    shortcuts = root.setdefault("shortcuts", {})
    target_key = None
    for k, entry in shortcuts.items():
        if isinstance(entry, dict) and str(_ci_get(entry, "AppName") or "").strip() == app_name:
            target_key = k
            break
    if target_key is None:
        nxt = max((int(k) for k in shortcuts if str(k).isdigit()), default=-1) + 1
        target_key = str(nxt)
    shortcuts[target_key] = {
        "appid": _shortcut_appid(exe, app_name),
        "AppName": app_name,
        "Exe": exe,
        "StartDir": start_dir,
        "icon": icon,
        "ShortcutPath": "",
        "LaunchOptions": launch_options,
        "IsHidden": 0,
        "AllowDesktopConfig": 1,
        "AllowOverlay": 1,
        "OpenVR": 0,
        "Devkit": 0,
        "DevkitGameID": "",
        "DevkitOverrideAppID": 0,
        "LastPlayTime": 0,
        "FlatpakAppID": "",
        "tags": {str(i): t for i, t in enumerate(tags or [])},
    }
    return int(target_key)


def _find_entry(root: dict, app_name: str) -> str | None:
    """Key under ``root['shortcuts']`` of the entry named ``app_name``."""
    shortcuts = root.get("shortcuts")
    if not isinstance(shortcuts, dict):
        return None
    for k, entry in shortcuts.items():
        if isinstance(entry, dict) and str(_ci_get(entry, "AppName") or "").strip() == app_name:
            return k
    return None


def remove(root: dict, *, app_name: str) -> bool:
    """Drop the shortcut named ``app_name`` (the exact inverse of ``add_or_update``,
    so add-then-remove leaves the file byte-identical). Other entries keep
    their indices; Steam renumbers on its next save. Returns whether anything
    was removed."""
    key = _find_entry(root, app_name)
    if key is None:
        return False
    del root["shortcuts"][key]
    return True


def modsync_target(flatpak_id: str | None = MODSYNC_FLATPAK_ID) -> tuple[str, str, str]:
    """(Exe, StartDir, LaunchOptions) for launching ModSync — Flatpak by default,
    falling back to the native command. Exe/StartDir are quoted as Steam stores
    them."""
    if flatpak_id:
        flatpak = shutil.which("flatpak") or "/usr/bin/flatpak"
        return f'"{flatpak}"', f'"{Path(flatpak).parent}"', f"run {flatpak_id}"
    exe = shutil.which("modsync")
    if exe:
        return f'"{exe}"', f'"{Path(exe).parent}"', ""
    return f'"{sys.executable}"', f'"{Path(sys.executable).parent}"', "-m modsync"


def steam_is_running() -> bool:
    """Best-effort (Linux) check: editing shortcuts.vdf while Steam is running is
    unsafe because Steam rewrites the file from memory when it exits.

    A Flatpak has its own PID namespace, so ``/proc`` shows nothing of the host
    there; ask the host through the Flatpak portal instead."""
    if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
        try:
            result = subprocess.run(
                ["flatpak-spawn", "--host", "pgrep", "-x", "steam"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode in (0, 1):
            return result.returncode == 0
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text().strip() == "steam":
                return True
        except OSError:
            continue
    return False


MODSYNC_APP_NAME = "ModSync"


def _user_shortcut_files() -> list[Path]:
    """``shortcuts.vdf`` for every Steam user on this machine (whether or not
    the file exists yet)."""
    out: list[Path] = []
    for root_dir in platforms.current().steam_roots():
        userdata = root_dir / "userdata"
        if not userdata.is_dir():
            continue
        for user in sorted(userdata.iterdir()):
            if user.name == "0" or not user.is_dir():
                continue
            out.append(user / "config" / "shortcuts.vdf")
    return out


def _save(vdf_path: Path, root_obj: dict) -> None:
    vdf_path.parent.mkdir(parents=True, exist_ok=True)
    if vdf_path.exists():
        # Steam has no recovery for a bad shortcuts.vdf: keep the
        # previous file next to it so a user can always roll back.
        shutil.copy2(vdf_path, vdf_path.with_suffix(".vdf.modsync-bak"))
    dump(vdf_path, root_obj)


def add_modsync_to_steam(
    flatpak_id: str | None = MODSYNC_FLATPAK_ID,
    *,
    create_missing: bool = True,
) -> list[Path]:
    """Add/update a "ModSync" non-Steam shortcut for every Steam user on this
    machine. Returns the shortcuts.vdf paths written. Steam must be restarted to
    pick the shortcut up."""
    exe, start_dir, opts = modsync_target(flatpak_id)
    written: list[Path] = []
    for vdf_path in _user_shortcut_files():
        if not vdf_path.exists() and not create_missing:
            continue
        root_obj = load(vdf_path) if vdf_path.exists() else {"shortcuts": {}}
        add_or_update(
            root_obj,
            app_name=MODSYNC_APP_NAME,
            exe=exe,
            start_dir=start_dir,
            launch_options=opts,
        )
        _save(vdf_path, root_obj)
        written.append(vdf_path)
    return written


def remove_modsync_from_steam() -> list[Path]:
    """Remove the "ModSync" shortcut from every Steam user's shortcuts.vdf.
    Returns the paths that actually had one. Steam must be restarted to notice."""
    written: list[Path] = []
    for vdf_path in _user_shortcut_files():
        if not vdf_path.exists():
            continue
        root_obj = load(vdf_path)
        if remove(root_obj, app_name=MODSYNC_APP_NAME):
            _save(vdf_path, root_obj)
            written.append(vdf_path)
    return written
