"""Find and settle the conflicts Syncthing leaves behind.

When two machines change the same file before they sync, Syncthing keeps the
newer one under the real name and the other next to it as
``<name>.sync-conflict-<date>-<time>-<device><ext>``. MO2 never reads those
copies, so a mod enabled on the machine that lost silently stays disabled.

The name doesn't say whose change the copy holds. ``<device>`` and the time are
the machine that found the conflict and when (checked with Syncthing 2.1: on
both machines the copy of A's change carried B's id), and the copy reaches every
machine, so on the machine that made that change it holds its own edit. The two
are only "the version in use" and "the other version", and the copy's own
modification time, which Syncthing keeps, says when that version was changed.

ModSync only finds them and lets the user keep one version or the other, whole.
It never merges the two: MO2 writes ``modlist.txt`` in reverse priority order
and ignores a name after its first line, and without the version both machines
started from, a two-file comparison can't tell a lost change from an
intentional one. The version not kept goes to ``.modsync-conflicts`` in the
instance, which ``.stignore`` keeps out of sync, rather than being deleted.

Every folder that syncs is searched (see ``stignore.CONTENT_DIRS``), plus the
files at the instance root. ``mods/`` can hold hundreds of thousands of files,
so the walk only reads names; callers that poll pace themselves by how long it
took (``ModSyncService._conflict_count``).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from modsync.sync.stignore import CONTENT_DIRS

ARCHIVE_DIR = ".modsync-conflicts"
SEARCHED_DIRS = CONTENT_DIRS
# Files read as INI (key = value under [sections]) to describe a difference:
# mod and game settings, and the .meta file MO2 writes next to each download.
INI_SUFFIXES = (".ini", ".meta")

_CONFLICT_RE = re.compile(
    r"^(?P<stem>.+?)\.sync-conflict-(?P<date>\d{8})-(?P<time>\d{6})-(?P<device>[A-Z2-7]{7})(?P<ext>\.[^.]*)?$"
)


@dataclass(frozen=True)
class Conflict:
    path: Path  # the copy Syncthing set aside
    original: Path  # the file under the real name, which MO2 uses
    device: str  # short id of the machine that found the conflict (not who made either change)
    when: datetime | None  # when it was found

    @property
    def changed(self) -> datetime | None:
        """When the other version was last changed."""
        try:
            return datetime.fromtimestamp(self.path.stat().st_mtime)
        except OSError:
            return None

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
    # Profile lists first: they decide which mods load.
    return sorted(found, key=lambda c: (c.kind == "file", c.relative(instance), c.when or datetime.min))


def related(found: list[Conflict], first: Conflict) -> list[Conflict]:
    """``first`` and the conflicts to decide with it. A profile's mod list,
    plugins and load order go together: keeping one machine's mod list with
    the other's plugins makes a profile neither machine had. Grouped only when
    the same machine found each conflict, one per file."""
    if first.kind == "file":
        return [first]
    group: list[Conflict] = []
    for c in found:
        if (c.kind != "file" and c.original.parent == first.original.parent and c.device == first.device
                and all(g.original != c.original for g in group)):
            group.append(c)
    return group


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


def differences(conflict: Conflict) -> list[str]:
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
            lines.append(f"Only in the other version: {_listing(only_there)}.")
        if only_here:
            lines.append(f"Only in the version in use: {_listing(only_here)}.")
        if on_there:
            lines.append(f"Enabled only in the other version: {_listing(on_there)}.")
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
        return ["Only the other version is left."]
    if here.st_size == there.st_size and conflict.original.read_bytes() == conflict.path.read_bytes():
        return ["Both versions are identical."]
    if conflict.original.suffix.lower() in INI_SUFFIXES:
        lines = _ini_differences(conflict)
        if lines:
            return lines
    return [f"The contents differ ({here.st_size:,} bytes in use, {there.st_size:,} in the other)."]


def _ini(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """``(section, key)`` → ``(written key, value)``, both matched without case
    as Skyrim and MO2 read them. A later line wins over an earlier one."""
    values: dict[tuple[str, str], tuple[str, str]] = {}
    section = ""
    for line in _read_lines(path):
        if line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
        elif "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            values[(section.lower(), key.lower())] = (f"[{section}] {key}" if section else key, value.strip())
    return values


def _ini_differences(conflict: Conflict) -> list[str]:
    """Settings added, removed or changed, or nothing when either side isn't INI."""
    here, there = _ini(conflict.original), _ini(conflict.path)
    if not here or not there:
        return []
    only_there = [there[k][0] for k in there if k not in here]
    only_here = [here[k][0] for k in here if k not in there]
    changed = [f"{here[k][0]} ({here[k][1] or 'empty'} in use, {there[k][1] or 'empty'} in the other)"
               for k in here if k in there and here[k][1] != there[k][1]]
    lines = []
    if only_there:
        lines.append(f"Settings only in the other version: {_listing(only_there)}.")
    if only_here:
        lines.append(f"Settings only in the version in use: {_listing(only_here)}.")
    if changed:
        lines.append(f"Set differently: {_listing(changed, limit=4)}.")
    return lines or ["Both versions have the same settings, written differently."]


# --- settling them ------------------------------------------------------------------
def new_archive(instance: Path | str) -> Path:
    """A folder of its own under ``.modsync-conflicts`` for one decision, so
    nothing moved there can replace something moved there before."""
    root = Path(instance) / ARCHIVE_DIR
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    n = 1
    while True:
        archive = root / (stamp if n == 1 else f"{stamp}-{n}")
        try:
            archive.mkdir(parents=True)
            return archive
        except FileExistsError:
            n += 1


def resolve(instance: Path | str, conflict: Conflict, keep: str, *, archive: Path | None = None) -> Path:
    """Keep ``"current"`` (the file in use) or ``"other"`` (the copy set aside).
    The version not kept moves into ``archive`` (a ``new_archive`` by default)
    with its path in the instance. Returns where it went."""
    if keep not in ("current", "other"):
        raise ValueError(f"keep must be 'current' or 'other', not {keep!r}")
    instance = Path(instance)
    archive = archive or new_archive(instance)
    if keep == "current":
        dest = archive / conflict.path.relative_to(instance)
        if dest.exists():
            raise FileExistsError(f"{dest} is already archived")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(conflict.path, dest)
        return dest
    dest = archive / conflict.original.relative_to(instance)
    if conflict.original.exists():
        if dest.exists():
            raise FileExistsError(f"{dest} is already archived")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(conflict.original, dest)
    os.replace(conflict.path, conflict.original)
    return dest
