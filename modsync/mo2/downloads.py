"""Handing a Nexus download (an ``nxm://`` link, see :mod:`modsync.nxm`) to a
game's Mod Organizer 2, whichever store launches the game.

A link reaches MO2 in one of three ways. An MO2 that is already open gets it
from a second ``ModOrganizer.exe``, which passes the link to the first and
exits. A closed one is opened with the link: through the store's own launch
while ModSync's Play hook for the store is on, so the session is the store's
(cloud saves, playtime), otherwise by ModSync directly. Each store supplies
those steps as a :class:`Target`, and :func:`download` picks one.

Through the store, ModSync leaves the link in a note for the hook. The hook
moves the note aside and empties it, so the moved file's time says when MO2's
launch began. A link that comes while MO2 is still starting for an earlier
one waits for it rather than launching again: the store ignores a second
launch, and a second note would replace the first. So does a link that comes
while the store is launching the game for something else (its Play button):
the store would ignore a launch for it, and the note would wait for the next
Play.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from modsync import background, config

# How long a note left for a hook stays good: long enough for the store to
# start and launch the game, short enough that a launch that never came
# doesn't turn a later Play into something else.
NOTE_TTL = 180

# How long MO2 gets to show up once its launch has begun (the hook took the
# link ModSync left, or ModSync started it directly), before a later link
# opens it again.
MO2_START_WAIT = 45.0

# How long a link waits for MO2 while the store launches the game for
# something else, such as ModSync's window waiting on a choice after Play.
BUSY_WAIT = 300.0


class Target(Protocol):
    """One game's MO2 as a download reaches it."""

    record: Path  # when ModSync last started this MO2 (the file's time), and how: "hook" or "direct"
    note: Path  # the link left for the store's hook
    taken: Path  # where the hook moves a note it takes

    def running(self) -> bool:
        """Whether this MO2 is open, or running the game."""

    def pass_link(self, url: str) -> str:
        """Give the open MO2 the link. Returns what happened."""

    def launching(self) -> str | None:
        """What the store is doing with the game already (launching it, or
        running it) that a launch for the link would collide with, or None."""

    def through_hook(self) -> bool:
        """Whether the store's launch of the game would run MO2 (the hook is on)."""

    def ask_hook(self, url: str) -> str:
        """Leave the link in :attr:`note` and have the store launch the game.
        Takes the note out again when the launch couldn't be asked for.
        Returns what happened."""

    def open_direct(self, url: str) -> str:
        """Start MO2 with the link. Returns what happened."""


def download(target: Target, url: str, *, poll: float = 2.0) -> str:
    """Hand a Nexus download to the target's MO2 (see the module's
    docstring). Returns what happened."""
    busy_since: float | None = None
    while True:
        with launch_lock(target.record) as record:
            if target.running():
                record.truncate(0)  # MO2 is up: no launch is under way any more
                return target.pass_link(url)
            if not under_way(record, target.note, target.taken):
                busy = target.launching()
                if busy is None:
                    if target.through_hook():
                        with launched(record, "hook"):
                            return target.ask_hook(url)
                    with launched(record, "direct"):
                        return target.open_direct(url)
                busy_since = time.monotonic() if busy_since is None else busy_since
                if time.monotonic() - busy_since >= BUSY_WAIT:
                    raise RuntimeError(f"{busy}, and Mod Organizer 2 didn't open for the download. "
                                       "Click it again once MO2 is open.")
        time.sleep(poll)


def shared_dir() -> Path:
    """Where the nxm:// handler, the hook and ModSync's own launches meet: a
    folder the Flatpak and a copy run on the host share (config.data_dir),
    unlike the state folder, which is each copy's own."""
    return config.data_dir() / "launches"


# --- the launch record ------------------------------------------------------------


@contextmanager
def launch_lock(path: Path):
    """The launch record, locked: one launch at a time decides whether to
    start MO2."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as record:
        fcntl.flock(record, fcntl.LOCK_EX)
        record.seek(0)
        yield record


@contextmanager
def launched(record, how: str):
    """Record a launch before starting it: the store may run the hook, which
    takes the note, before the launch request even returns, and the note
    counts only when taken after the record. A launch that fails to start is
    taken out again, so nothing waits for it."""
    record.truncate(0)
    record.write(f"{time.time()} {how}\n")
    record.flush()
    try:
        yield
    except BaseException:
        record.truncate(0)
        record.flush()
        raise


def under_way(record, note: Path, taken: Path) -> bool:
    """Whether the locked launch ``record`` may still bring MO2 up."""
    record.seek(0)
    return launch_under_way(record.read(), os.fstat(record.fileno()).st_mtime, note, taken, time.time())


def launch_under_way(record: str, recorded_at: float, note: Path, taken: Path, now: float) -> bool:
    """Whether the launch ``record`` (written at ``recorded_at``) may still
    bring MO2 up. Every waiting link reads the same times, so they all stop
    waiting together."""
    if not record.strip() or now >= recorded_at + NOTE_TTL:
        return False
    if record.split()[1:] != ["hook"]:  # MO2 started directly
        return now < recorded_at + MO2_START_WAIT
    if note.exists():  # waiting for the store to run the hook
        return True
    try:
        taken_at = taken.stat().st_mtime
    except OSError:
        return False  # the link went without the hook taking it
    return taken_at >= recorded_at and now < taken_at + MO2_START_WAIT


# --- MO2 in a Wine prefix -----------------------------------------------------------


def local_path(prefix: Path, windows: str) -> Path | None:
    """A Windows path in a Wine prefix as the Linux one, through the prefix's
    own drive links: umu maps the home folder to X:, Proton maps / to Z:."""
    drive, _, rest = windows.partition(":")
    try:
        root = (prefix / "dosdevices" / f"{drive.lower()}:").resolve(strict=True)
    except OSError:
        return None
    return root.joinpath(*[part for part in rest.split("\\") if part])


def process_prefix(pid: str) -> Path | None:
    """The Wine prefix a process runs in, from its environment, or None when
    that can't be read."""
    try:
        result = background.run_host(["cat", f"/proc/{pid}/environ"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for entry in result.stdout.split("\0") if result.returncode == 0 else []:
        if entry.startswith("WINEPREFIX=") and len(entry) > len("WINEPREFIX="):
            return Path(entry[len("WINEPREFIX="):])
    return None


def mo2_running(prefix: Path, instance: Path) -> bool:
    """Whether this instance's MO2 is running in the Wine prefix ``prefix``
    (the folder holding ``drive_c``), open or running the game. Wine names its
    processes by their Windows path, which only means this instance through
    this prefix's drive letters, so a match must also run in this prefix
    (when its environment can be read)."""
    try:
        found = background.run_host(["pgrep", "-af", r"^[A-Za-z]:\\.*\\ModOrganizer\.exe"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    target = str((instance / "ModOrganizer.exe").resolve()).lower()
    wine_prefix = prefix.resolve()
    for line in found.stdout.splitlines():
        pid, _, command = line.partition(" ")
        match = re.match(r"([A-Za-z]:\\(?:.*?\\)?ModOrganizer\.exe)(?:\s|$)", command, re.IGNORECASE)
        local = local_path(prefix, match[1]) if match else None
        if local is None or str(local.resolve()).lower() != target:
            continue
        found_prefix = process_prefix(pid)
        if found_prefix is None or found_prefix.resolve() == wine_prefix:
            return True
    return False


def take_note(note: Path, taken: Path) -> str | None:
    """For a hook written in Python: take the note ModSync left, once, the way
    Heroic's hook script does (move it aside, then empty it so the moved
    file's time is when MO2's launch began). Returns the link, or None when
    there was none or it's older than :data:`NOTE_TTL`."""
    try:
        os.replace(note, taken)
    except OSError:
        return None
    try:
        left = taken.stat().st_mtime
        with taken.open("r+", encoding="utf-8") as f:
            text = f.readline().strip()
            f.truncate(0)
        os.utime(taken)
    except OSError:
        return None
    if time.time() - left > NOTE_TTL or not text.lower().startswith("nxm://"):
        return None
    return text
