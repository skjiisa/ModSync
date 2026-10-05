"""Find and settle the conflicts Syncthing leaves behind.

When two machines change the same file before they sync, Syncthing keeps the
newer one under the real name and the other next to it as
``<name>.sync-conflict-<date>-<time>-<device><ext>``. MO2 never reads those
copies, so a mod enabled on the machine that lost silently stays disabled.

ModSync only finds them and lets the user keep one version or the other, whole.
It never merges the two: MO2 writes ``modlist.txt`` in reverse priority order
and ignores a name after its first line, and without the version both machines
started from, a two-file comparison can't tell a lost change from an
intentional one. The version not kept goes to ``.modsync-conflicts`` in the
instance, which ``.stignore`` keeps out of sync, rather than being deleted.

Only ``profiles/``, ``overwrite/`` and the files at the instance root are
searched. ``mods/`` can hold hundreds of thousands of files and is rarely
edited on both machines.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ARCHIVE_DIR = ".modsync-conflicts"
SEARCHED_DIRS = ("profiles", "overwrite")

_CONFLICT_RE = re.compile(
    r"^(?P<stem>.+?)\.sync-conflict-(?P<date>\d{8})-(?P<time>\d{6})-(?P<device>[A-Z2-7]{7})(?P<ext>\.[^.]*)?$"
)


@dataclass(frozen=True)
class Conflict:
    path: Path  # the copy Syncthing set aside
    original: Path  # the file under the real name, which MO2 uses
    device: str  # short id of the machine whose change was set aside
    when: datetime | None

    @property
    def kind(self) -> str:
        """"modlist" | "plugins" | "loadorder" | "file"."""
        name = self.original.name.lower()
        if self.original.parent.parent.name == "profiles":
            return {"modlist.txt": "modlist", "plugins.txt": "plugins", "loadorder.txt": "loadorder"}.get(
                name, "file")
        return "file"

    def relative(self, instance: Path | str) -> str:
        return self.original.relative_to(instance).as_posix()


def parse(path: Path) -> Conflict | None:
    m = _CONFLICT_RE.match(path.name)
    if not m:
        return None
    try:
        when = datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S")
    except ValueError:
        when = None
    return Conflict(path, path.with_name(m["stem"] + (m["ext"] or "")), m["device"], when)


def find(instance: Path | str) -> list[Conflict]:
    instance = Path(instance)
    candidates: list[Path] = []
    try:
        candidates += [p for p in instance.iterdir() if p.is_file()]
    except OSError:
        return []
    for name in SEARCHED_DIRS:
        for root, _dirs, files in os.walk(instance / name):
            candidates += [Path(root) / f for f in files if ".sync-conflict-" in f]
    found = [c for c in map(parse, candidates) if c is not None]
    return sorted(found, key=lambda c: (c.relative(instance), c.when or datetime.min))


# --- describing the difference ---------------------------------------------------
def _read_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _modlist(path: Path) -> tuple[list[str], set[str]]:
    """Names in file order, and the enabled ones. ``*`` lines (DLC and other
    files MO2 doesn't manage) can't be switched, so they count as neither."""
    names: list[str] = []
    enabled: set[str] = set()
    for line in _read_lines(path):
        if line[0] not in "+-" or line[1:] in names:
            continue  # MO2 ignores a name after its first line
        names.append(line[1:])
        if line[0] == "+":
            enabled.add(line[1:])
    return names, enabled


def _plugins(path: Path) -> tuple[list[str], set[str]]:
    names: list[str] = []
    seen: set[str] = set()
    enabled: set[str] = set()
    for line in _read_lines(path):
        name = line.lstrip("*")
        if name.lower() in seen:
            continue  # plugin names are case-insensitive
        seen.add(name.lower())
        names.append(name)
        if line.startswith("*"):
            enabled.add(name)
    return names, enabled


def _listing(names: list[str], limit: int = 6) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def differences(conflict: Conflict, other: str = "the other machine") -> list[str]:
    """What differs between the version in use and the one set aside, in plain
    words. Describes, never judges: either side may be the intended one."""
    kind = conflict.kind
    if kind in ("modlist", "plugins"):
        reader = _modlist if kind == "modlist" else _plugins
        key = (lambda n: n) if kind == "modlist" else str.lower  # plugin names ignore case
        here_names, here_on = reader(conflict.original)
        there_names, there_on = reader(conflict.path)
        what = "mods" if kind == "modlist" else "plugins"
        here_on, there_on = {key(n) for n in here_on}, {key(n) for n in there_on}
        here_set, there_set = {key(n) for n in here_names}, {key(n) for n in there_names}
        lines = []
        only_there = [n for n in there_names if key(n) not in here_set]
        only_here = [n for n in here_names if key(n) not in there_set]
        on_there = [n for n in there_names if key(n) in there_on and key(n) in here_set - here_on]
        on_here = [n for n in here_names if key(n) in here_on and key(n) in there_set - there_on]
        if only_there:
            lines.append(f"Only in the version from {other}: {_listing(only_there)}.")
        if only_here:
            lines.append(f"Only in the version in use: {_listing(only_here)}.")
        if on_there:
            lines.append(f"Enabled only in the version from {other}: {_listing(on_there)}.")
        if on_here:
            lines.append(f"Enabled only in the version in use: {_listing(on_here)}.")
        if [key(n) for n in here_names if key(n) in there_set] != [key(n) for n in there_names if key(n) in here_set]:
            lines.append(f"The {what} in both are in a different order.")
        return lines or [f"Both versions list the same {what} the same way."]
    if kind == "loadorder":
        same = _read_lines(conflict.original) == _read_lines(conflict.path)
        return ["The load order is the same."] if same else ["The load order is different."]
    try:
        here, there = conflict.original.stat(), conflict.path.stat()
    except OSError:
        return ["Only the version from the other machine is left."]
    if here.st_size == there.st_size and conflict.original.read_bytes() == conflict.path.read_bytes():
        return ["Both versions are identical."]
    return [f"The contents differ ({here.st_size:,} bytes in use, {there.st_size:,} from {other})."]


# --- settling one ------------------------------------------------------------------
def resolve(instance: Path | str, conflict: Conflict, keep: str, *, stamp: str | None = None) -> Path:
    """Keep ``"current"`` (the file in use) or ``"other"`` (the copy set aside).
    The version not kept moves under ``.modsync-conflicts/<stamp>/`` with its
    path in the instance. Returns where it went."""
    if keep not in ("current", "other"):
        raise ValueError(f"keep must be 'current' or 'other', not {keep!r}")
    instance = Path(instance)
    archive = instance / ARCHIVE_DIR / (stamp or datetime.now().strftime("%Y%m%d-%H%M%S"))
    if keep == "current":
        dest = archive / conflict.path.relative_to(instance)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(conflict.path, dest)
        return dest
    dest = archive / conflict.original.relative_to(instance)
    if conflict.original.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(conflict.original, dest)
    os.replace(conflict.path, conflict.original)
    return dest
