"""Whether someone is playing on this machine, so the background service can
pause ModSync's folders in Syncthing meanwhile (see ``serve.Server``).

Playing means a game runs and no ModSync window is open:

* a game: anything Steam launched (its ``reaper SteamLaunch`` wrapper, native
  or Proton, a non-Steam shortcut too); a Windows program outside
  ``C:\\windows`` that Heroic's launch of a game started (Wine's own services
  can keep that launch alive after the game, so it alone doesn't count); Mod
  Organizer 2 under Wine however it started; or Daggerfall Unity on its own.
  Another Windows program, an installer say, doesn't count. One ``ps`` on
  the host tells, the same inside the Flatpak, which can't see the host's
  processes itself.
* a window: an open window says someone is using ModSync, Steam's Play hook
  counting down before the game too, so syncing goes on. Each window holds a
  shared lock on ``window.lock`` in ModSync's data folder while it's open,
  which the service tests without waiting.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass

from modsync import config

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Linux
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

WINDOW_LOCK = "window.lock"

# Steam starts every game, native or Proton, under its reaper. On ARM (the
# Steam Frame) FEX-Emu runs it: then the process is named after FEX, and its
# command line is still the program's.
_EMULATORS = ("FEX", "FEXInterpreter")
_STEAM_LAUNCH = re.compile(r"/reaper SteamLaunch(?:\s|$)")
_DFU = re.compile(r"/DaggerfallUnity\.x86_64(?:\s|$)")
# Heroic's launch of a game: gogdl (GOG), legendary (Epic) or nile (Amazon).
_HEROIC_LAUNCH = re.compile(r"^(?:\S*/)?(?:python3?\s+)?(?:\S*/)?(?:gogdl|legendary|nile)\s(?:.*\s)?launch\s")
# Wine names a Windows process by its program's Windows path.
_WINDOWS_PROGRAM = re.compile(r"^[a-z]:\\(?!windows\\)", re.IGNORECASE)
_MO2 = re.compile(r"^[a-z]:\\.*\\ModOrganizer\.exe(?:\s|$)", re.IGNORECASE)
_COMM_WIDTH = 16  # the name, padded by ps to 15 characters (where Linux cuts it), and a space

_window_lock: int | None = None


def _lock_path():
    return config.data_dir() / WINDOW_LOCK


def hold_window_lock() -> None:
    """Called once by a window when it opens: the lock lasts as long as the
    process. Programs it starts don't inherit it (Python opens files
    close-on-exec), so MO2 started from a window doesn't hold it."""
    global _window_lock
    if fcntl is None or _window_lock is not None:
        return
    try:
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_SH)
    except OSError as exc:  # the service then pauses while someone plays with ModSync open
        log.warning("could not hold %s: %s", WINDOW_LOCK, exc)
        return
    _window_lock = fd


def release_window_lock() -> None:
    global _window_lock
    if _window_lock is not None:
        os.close(_window_lock)  # closing the only descriptor releases the lock
        _window_lock = None


def window_open() -> bool:
    """A ModSync window is open on this machine, in this setup."""
    if fcntl is None:
        return False
    try:
        fd = os.open(_lock_path(), os.O_RDWR)
    except OSError:
        return False  # no window has opened since the data folder was made
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:  # BlockingIOError: a window holds it
        return True
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    comm: str  # the name, cut to 15 characters
    args: str  # the command line, its arguments joined with spaces


def games_in(processes: list[Process]) -> bool:
    """Whether these processes include a game (see the module's docstring)."""
    parent = {p.pid: p.ppid for p in processes}
    heroic = set()
    for p in processes:
        emulated = p.comm in _EMULATORS
        if (p.comm == "reaper" or emulated) and _STEAM_LAUNCH.search(p.args):
            return True
        if p.comm == "DaggerfallUnity" or (emulated and _DFU.search(p.args)):
            return True
        if _MO2.match(p.args):
            return True
        if _HEROIC_LAUNCH.match(p.args):
            heroic.add(p.pid)
    if not heroic:
        return False
    for p in processes:
        if _WINDOWS_PROGRAM.match(p.args):
            pid, seen = p.ppid, set()
            while pid > 1 and pid not in seen:
                if pid in heroic:
                    return True
                seen.add(pid)
                pid = parent.get(pid, 0)
    return False


_PS_LINE = re.compile(r"^\s*(\d+)\s+(\d+) (.{%d}) ?(.*)$" % (_COMM_WIDTH - 1))


def _parse_ps(output: str) -> list[Process]:
    out = []
    for line in output.splitlines():
        m = _PS_LINE.match(line)
        if m:
            out.append(Process(int(m[1]), int(m[2]), m[3].strip(), m[4].strip()))
    return out


def game_running() -> bool:
    """A game runs on this machine. False when the host can't be asked."""
    from modsync.background import run_host

    try:
        result = run_host(["ps", "-e", "-ww", "-o", f"pid=,ppid=,comm:{_COMM_WIDTH - 1}=,args="], timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("could not list the host's processes: %s", exc)
        return False
    if result.returncode != 0:
        log.info("ps failed: %s", (result.stderr or "").strip())
        return False
    return games_in(_parse_ps(result.stdout))


def playing() -> bool:
    """A game runs and no ModSync window is open. The window is checked first:
    it costs nothing, and a window open means there's no need to ask the host."""
    return not window_open() and game_running()
