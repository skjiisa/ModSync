"""Restore points: a bounded local history of the MO2 instance's profiles.

A restore point holds every profile's lists and settings as they were at one
moment: the mod list, plugins, load order, archives and locked plugin order,
and the profile's INI files (MO2's own ``settings.ini`` and the game's INIs a
profile keeps for itself). Restoring puts one profile back whole, so its mod
list, plugins and load order always come from the same moment.

Only profile text is kept. A mod folder deleted since, a game update or a save
made with other mods are out of its reach: the preview names the mods a
restored profile lists that aren't in ``mods/`` any more, and a restore never
claims to bring back a working setup.

Restore points live in ModSync's data folder, one folder per instance, never in
the instance itself, so they don't sync. Each machine keeps its own. They are
taken when the profiles changed since the last one: by the background service
every few minutes while MO2 and the games are closed, and by the app before
Play or Open MO2, before a conflict choice, before copying from another machine
and before every restore. ``KEEP`` of them are kept per instance, newest first,
and no more than ``MAX_BYTES`` in all; a restore point is never cut short to
fit, only removed whole.

The versions not kept when settling a conflict (``.modsync-conflicts`` in the
instance) are offered too, where they hold a profile's lists, and a copy's
set-aside files (``.modsync-before-join``) are listed to be opened by hand.

A restore notes what it does in a journal before changing anything, so one cut
short (a crash, a power cut) is finished, or undone, the next time ModSync
looks (``finish_interrupted``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from modsync import config
from modsync.mo2 import instance as mo2_instance
from modsync.sync import conflicts

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None

log = logging.getLogger(__name__)

SCHEMA = 1
KEEP = 40  # restore points kept per instance
MAX_BYTES = 64 * 1024 * 1024  # all of an instance's together; the newest is always kept
MAX_FILE = 4 * 1024 * 1024  # a profile file bigger than this isn't a list or settings
# What a profile holds that decides which mods and plugins load, and how.
LIST_FILES = ("modlist.txt", "plugins.txt", "loadorder.txt", "archives.txt", "lockedorder.txt")
COPY_ARCHIVE_DIR = ".modsync-before-join"  # modsync.service's; repeated to keep this module light
JOURNAL = "restore-journal.json"

# Why a restore point was taken, in words.
REASONS = {
    "auto": "Profiles changed",
    "play": "Before playing",
    "conflict": "Before choosing a version",
    "copy": "Before copying from another machine",
    "restore": "Before a restore",
    "manual": "Saved by you",
}
_ID_RE = re.compile(r"^\d{8}-\d{6}(-\d+)?$")
_KINDS = {"modlist.txt": "modlist", "plugins.txt": "plugins", "loadorder.txt": "loadorder"}


class RestoreError(RuntimeError):
    """A restore can't go ahead, and nothing was changed."""


@dataclass(frozen=True)
class Point:
    """Something a profile can be restored from, or an archive to open by hand.

    ``kind`` is "point" (a restore point), "conflict" (the versions not kept
    when settling a conflict) or "copy" (files a copy set aside, opened by
    hand only). ``files`` maps each profile to its files, by name, where they
    are on disk. A restore point holds whole profiles: restoring one also
    removes profile files it didn't have. A conflict archive holds only the
    files that were decided, and restoring it replaces only those."""

    id: str
    kind: str
    path: Path
    created: datetime | None
    reason: str = ""
    files: dict[str, dict[str, Path]] = field(default_factory=dict)
    size: int = 0
    other_files: int = 0  # files in an archive that aren't a profile's lists

    @property
    def profiles(self) -> list[str]:
        return sorted(self.files, key=str.lower)

    @property
    def whole(self) -> bool:
        """Restoring a profile from this puts back all of its files."""
        return self.kind == "point"

    @property
    def restorable(self) -> bool:
        return bool(self.files)

    @property
    def described(self) -> str:
        """Why it exists, in words."""
        if self.kind == "conflict":
            return "Versions not kept when a file changed on two machines"
        if self.kind == "copy":
            return "Files set aside when copying from another machine"
        return REASONS.get(self.reason, "Saved")


@dataclass
class FileChange:
    name: str
    change: str  # "changed" | "added" (comes back) | "removed" (goes)
    details: list[str] = field(default_factory=list)


@dataclass
class Preview:
    """What restoring ``profile`` from ``point`` would change."""

    point: Point
    profile: str
    exists_now: bool  # the profile is in the instance now
    changes: list[FileChange] = field(default_factory=list)
    missing_mods: list[str] = field(default_factory=list)  # enabled in the restored list, not in mods/

    @property
    def unchanged(self) -> bool:
        return self.exists_now and not self.changes


@dataclass
class Usage:
    points: int
    bytes: int


# --- where things are ----------------------------------------------------------------------
def store_dir(instance: Path | str) -> Path:
    """This instance's restore points, in ModSync's data folder."""
    key = hashlib.sha256(os.path.abspath(str(instance)).encode()).hexdigest()[:16]
    return config.data_dir() / "restore-points" / key


def profiles_dir(instance: Path | str) -> Path:
    info = mo2_instance.inspect(instance)
    found = info.content_dirs.get("profiles")
    return found.path if found is not None and found.path is not None else Path(instance) / "profiles"


def _mods_dir(instance: Path | str) -> Path:
    info = mo2_instance.inspect(instance)
    found = info.content_dirs.get("mods")
    return found.path if found is not None and found.path is not None else Path(instance) / "mods"


def tracked(name: str) -> bool:
    """Whether a file in a profile folder is part of a restore point."""
    low = name.lower()
    if ".sync-conflict-" in low or low.startswith("."):
        return False
    return low in LIST_FILES or low.endswith(".ini")


def _profile_files(profile: Path) -> dict[str, Path]:
    try:
        entries = list(profile.iterdir())
    except OSError:
        return {}
    found = {}
    for path in entries:
        try:
            if tracked(path.name) and path.is_file() and path.stat().st_size <= MAX_FILE:
                found[path.name] = path
        except OSError:
            continue
    return found


def current(instance: Path | str) -> dict[str, dict[str, Path]]:
    """Every profile's tracked files as they are now."""
    root = profiles_dir(instance)
    try:
        folders = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return {}
    return {p.name: _profile_files(p) for p in folders}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- locking ------------------------------------------------------------------------------
@contextmanager
def _locked(instance: Path | str):
    """One process at a time takes, prunes or restores an instance's points."""
    store = store_dir(instance)
    store.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield store
        return
    with open(store / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield store
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- reading them back --------------------------------------------------------------------
def _read_meta(path: Path) -> dict | None:
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("profiles"), dict):
        return None
    if isinstance(meta.get("schema"), int) and meta["schema"] > SCHEMA:
        return None  # a newer ModSync's: leave it be
    return meta


def _load_point(path: Path) -> Point | None:
    meta = _read_meta(path)
    if meta is None:
        return None
    try:
        created = datetime.fromisoformat(str(meta.get("created", "")))
    except ValueError:
        created = None
    files: dict[str, dict[str, Path]] = {}
    for profile, names in meta["profiles"].items():
        if not isinstance(names, dict):
            continue
        files[profile] = {name: path / "profiles" / profile / name for name in names}
    return Point(id=path.name, kind="point", path=path, created=created, reason=str(meta.get("reason", "")),
                 files=files, size=int(meta.get("size", 0) or 0))


def _saved(instance: Path | str) -> list[Point]:
    """This instance's restore points, newest first."""
    store = store_dir(instance)
    try:
        dirs = [p for p in store.iterdir() if p.is_dir() and _ID_RE.match(p.name)]
    except OSError:
        return []
    return [pt for pt in map(_load_point, sorted(dirs, key=_order, reverse=True)) if pt is not None]


def _order(path: Path) -> tuple[str, int]:
    """Taken in the same second: "-2" after the first, "-10" after "-9"."""
    stamp, n = path.name[:15], path.name[16:]
    return stamp, int(n) if n.isdigit() else 1


def _stamp(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def _conflict_archives(instance: Path) -> list[Point]:
    """The versions not kept when settling conflicts, newest first. A
    profile's lists in one are restorable; the rest is there to open."""
    root = instance / conflicts.ARCHIVE_DIR
    try:
        archives = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    try:
        profiles_rel = profiles_dir(instance).relative_to(instance.resolve())
    except (ValueError, OSError):
        profiles_rel = None
    found = []
    for archive in archives:
        files: dict[str, dict[str, Path]] = {}
        other = size = 0
        for path in archive.rglob("*"):
            if not path.is_file():
                continue
            try:
                size += path.stat().st_size
            except OSError:
                pass
            rel = path.relative_to(archive)
            parsed = conflicts.parse(path)
            name = parsed.original.name if parsed is not None else path.name
            if (profiles_rel is not None and rel.parent.parent == profiles_rel and tracked(name)
                    and name not in files.get(rel.parent.name, {})):
                files.setdefault(rel.parent.name, {})[name] = path
            else:
                other += 1
        if files or other:
            found.append(Point(id=f"conflict-{archive.name}", kind="conflict", path=archive,
                               created=_stamp(archive.name), files=files, size=size, other_files=other))
    return found


def _copy_archives(instance: Path) -> list[Point]:
    root = instance / COPY_ARCHIVE_DIR
    try:
        archives = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    found = []
    for archive in archives:
        count = size = 0
        for path in archive.rglob("*"):
            if path.is_file():
                count += 1
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
        if count:
            try:
                created = datetime.fromtimestamp(archive.stat().st_mtime)
            except OSError:
                created = None
            found.append(Point(id=f"copy-{archive.name}", kind="copy", path=archive, created=created,
                               size=size, other_files=count))
    return found


def points(instance: Path | str, *, archives: bool = True) -> list[Point]:
    """Restore points, newest first, then the conflict archives and the
    copies' set-aside files (with ``archives``), newest first."""
    instance = Path(instance)
    found = _saved(instance)
    if archives:
        extra = _conflict_archives(instance) + _copy_archives(instance)
        found += sorted(extra, key=lambda p: p.created or datetime.min, reverse=True)
    return found


def find(instance: Path | str, point_id: str) -> Point:
    for point in points(instance):
        if point.id == point_id:
            return point
    raise RestoreError(f"No restore point {point_id}. 'modsync restore' lists them.")


def usage(instance: Path | str) -> Usage:
    saved = _saved(instance)
    return Usage(points=len(saved), bytes=sum(p.size for p in saved))


# --- taking them --------------------------------------------------------------------------
def _new_id(store: Path, now: datetime) -> str:
    """An id that sorts after every point there is, even one taken in the
    same second or with the clock set back since (when it was taken is in
    its meta.json)."""
    stamp = now.strftime("%Y%m%d-%H%M%S")
    try:
        taken = [_order(p) for p in store.iterdir() if _ID_RE.match(p.name) or _ID_RE.match(p.name[5:])]
    except OSError:
        taken = []
    newest = max(taken, default=("", 0))
    if newest[0] < stamp:
        return stamp
    return f"{newest[0]}-{newest[1] + 1}"


def _fingerprint(profiles: dict[str, dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(profiles, sort_keys=True).encode()).hexdigest()


def capture(instance: Path | str, reason: str = "auto", *, force: bool = False) -> Point | None:
    """Take a restore point of every profile, unless they're the same as in
    the newest one (or ``force``). Returns the new point, or None."""
    instance = Path(instance)
    if not instance.is_dir():
        return None
    with _locked(instance) as store:
        return _capture(instance, store, reason, force=force)


def _capture(instance: Path, store: Path, reason: str, *, force: bool = False) -> Point | None:
    now = current(instance)
    if not now:
        return None
    hashes: dict[str, dict[str, str]] = {}
    for profile, files in now.items():
        hashes[profile] = {}
        for name, path in files.items():
            try:
                hashes[profile][name] = _digest(path)
            except OSError:
                continue
    fingerprint = _fingerprint(hashes)
    saved = _saved(instance)
    if saved and not force:
        newest = _read_meta(saved[0].path) or {}
        if newest.get("fingerprint") == fingerprint:
            return None
    for leftover in store.glob(".new-*"):  # a capture cut short
        shutil.rmtree(leftover, ignore_errors=True)
    stamp = datetime.now()
    point_id = _new_id(store, stamp)
    work = store / f".new-{point_id}"
    size = 0
    for profile, files in now.items():
        dest = work / "profiles" / profile
        dest.mkdir(parents=True, exist_ok=True)
        for name in list(hashes[profile]):
            try:
                shutil.copy2(files[name], dest / name)
            except OSError as exc:
                log.warning("restore point: couldn't copy %s/%s: %s", profile, name, exc)
                del hashes[profile][name]
                continue
            # What was copied, which may be newer than what was hashed.
            hashes[profile][name] = _digest(dest / name)
            size += (dest / name).stat().st_size
    meta = {"schema": SCHEMA, "created": stamp.isoformat(timespec="seconds"), "reason": reason,
            "instance": str(instance), "fingerprint": _fingerprint(hashes), "size": size,
            "profiles": hashes, "mods": _mod_names(instance)}
    (work / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    os.replace(work, store / point_id)
    log.info("restore point %s (%s) of %d profile(s) in %s", point_id, reason, len(hashes), instance)
    _prune(instance, store)
    return _load_point(store / point_id)


def _mod_names(instance: Path) -> list[str]:
    try:
        return sorted(p.name for p in _mods_dir(instance).iterdir() if p.is_dir())
    except OSError:
        return []


def _prune(instance: Path, store: Path) -> list[str]:
    """Keep the newest ``KEEP`` points, within ``MAX_BYTES``, and the ones a
    restore under way needs. Whole points only."""
    saved = _saved(instance)
    needed = set()
    journal = _read_journal(store)
    if journal is not None:
        needed = {journal.get("point"), journal.get("before")}
    removed = []
    total = 0
    for i, point in enumerate(saved):
        total += point.size
        if i == 0 or point.id in needed:
            continue
        if i >= KEEP or total > MAX_BYTES:
            shutil.rmtree(point.path, ignore_errors=True)
            removed.append(point.id)
            total -= point.size
    if removed:
        log.info("removed %d old restore point(s): %s", len(removed), ", ".join(removed))
    return removed


# --- what a restore would do ----------------------------------------------------------------
def _enabled_mods(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("+") and line[1:] not in names:
            names.append(line[1:])
    return names


def _same(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def preview(instance: Path | str, point: Point, profile: str) -> Preview:
    """What restoring ``profile`` from ``point`` would change, and which mods
    the restored list enables that aren't in the instance any more."""
    instance = Path(instance)
    if profile not in point.files:
        raise RestoreError(f"{point.id} has no profile {profile}.")
    here_dir = profiles_dir(instance) / profile
    exists = here_dir.is_dir()
    here = _profile_files(here_dir) if exists else {}
    here_low = {name.lower(): path for name, path in here.items()}
    wanted = point.files[profile]
    out = Preview(point=point, profile=profile, exists_now=exists)
    for name in sorted(wanted, key=str.lower):
        src = wanted[name]
        mine = here_low.get(name.lower())
        if mine is None:
            out.changes.append(FileChange(name, "added"))
        elif not _same(mine, src):
            kind = _KINDS.get(name.lower(), "file")
            out.changes.append(FileChange(name, "changed", conflicts.compare(
                kind, mine, src, ours="the profile now", theirs="the restore point")))
    if point.whole:
        names = {n.lower() for n in wanted}
        out.changes += [FileChange(name, "removed") for name in sorted(here, key=str.lower)
                        if name.lower() not in names]
    modlist = next((p for n, p in wanted.items() if n.lower() == "modlist.txt"), None)
    if modlist is not None:
        mods = _mods_dir(instance)
        out.missing_mods = [m for m in _enabled_mods(modlist) if not (mods / m).is_dir()]
    return out


# --- restoring -----------------------------------------------------------------------------
def _read_journal(store: Path) -> dict | None:
    try:
        data = json.loads((store / JOURNAL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pending(instance: Path | str) -> dict | None:
    """The journal of a restore that hasn't finished, or None."""
    return _read_journal(store_dir(instance))


def _write_journal(store: Path, data: dict) -> None:
    tmp = store / f".{JOURNAL}.tmp"
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, store / JOURNAL)


def _apply(instance: Path, point: Point, profile: str) -> list[str]:
    """Write ``profile``'s files from ``point`` into the instance. Each file is
    replaced whole; doing it again gives the same result. Returns the names
    written or removed."""
    target = profiles_dir(instance) / profile
    target.mkdir(parents=True, exist_ok=True)
    wanted = point.files[profile]
    here = _profile_files(target)
    done = []
    for name, path in here.items():
        # A file of the same name in another case goes (MO2 on Linux would see both),
        # and with a whole point, every file the point didn't have.
        same = [w for w in wanted if w.lower() == name.lower()]
        if (same and same[0] != name) or (not same and point.whole):
            path.unlink(missing_ok=True)
            done.append(name)
    for name, src in wanted.items():
        if not src.is_file():
            raise RestoreError(f"{src} is gone, so {profile} can't be restored from it.")
        # Syncthing never picks up its own temporary names, should it be scanning.
        tmp = target / f".syncthing.{name}.tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, target / name)
        done.append(name)
    return done


def begin(instance: Path | str, point: Point, profile: str, *, before: str, resume: str = "") -> None:
    """Note the restore about to happen, so ``finish_interrupted`` can see it
    through. ``before`` is the restore point of how things were; ``resume`` a
    Syncthing folder paused for the restore."""
    with _locked(instance) as store:
        _write_journal(store, {"point": point.id, "profile": profile, "before": before, "resume": resume})


def apply(instance: Path | str, point: Point, profile: str) -> list[str]:
    instance = Path(instance)
    with _locked(instance):
        return _apply(instance, point, profile)


def end(instance: Path | str) -> None:
    with _locked(instance) as store:
        (store / JOURNAL).unlink(missing_ok=True)


def roll_back(instance: Path | str, before: Point, profile: str) -> None:
    """Put ``profile`` back as ``before`` has it. A profile that wasn't there
    then goes again (its tracked files, and its folder once empty)."""
    instance = Path(instance)
    if profile in before.files:
        apply(instance, before, profile)
        return
    target = profiles_dir(instance) / profile
    with _locked(instance):
        for path in _profile_files(target).values():
            path.unlink(missing_ok=True)
        try:
            target.rmdir()
        except OSError:
            pass  # something else is in it


def finish(instance: Path | str, journal: dict) -> str:
    """Finish the restore ``journal`` describes: again from its point, or,
    when that has gone, back to how things were before. Returns what was done."""
    instance = Path(instance)
    profile = str(journal.get("profile", ""))
    for key, done in (("point", "finished"), ("before", "undone")):
        point_id = str(journal.get(key) or "")
        if not point_id or not profile:
            continue
        try:
            point = find(instance, point_id)
        except RestoreError:
            continue
        if key == "point" and profile not in point.files:
            continue
        try:
            if key == "point":
                apply(instance, point, profile)
            else:
                roll_back(instance, point, profile)
        except (OSError, RestoreError) as exc:
            log.warning("couldn't redo the restore of %s from %s: %s", profile, point_id, exc)
            continue
        log.info("restore of %s from %s was cut short; %s", profile, journal.get("point"), done)
        return done
    log.warning("restore of %s was cut short and neither %s nor %s can be read; left as it is",
                profile, journal.get("point"), journal.get("before"))
    return "left"


# --- in words ------------------------------------------------------------------------------
LIMITS = ("A restore point keeps only profiles' lists and settings. Restoring one can't bring back a mod "
          "that was deleted, an older game version or a save made with other mods.")


def when(point: Point) -> str:
    """When ``point`` was taken, short: "16 Nov 14:32"."""
    if point.created is None:
        return "Unknown time"
    return f"{point.created.day} {point.created:%b %H:%M}"


def size_words(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def preview_lines(preview: Preview) -> list[str]:
    """What restoring would change, a line per change, then what it can't do."""
    if preview.unchanged:
        return [f"Profile {preview.profile} is the same now. Restoring it changes nothing."]
    lines = []
    if not preview.exists_now:
        lines.append(f"Profile {preview.profile} isn't in the instance now. Restoring it brings it back.")
    for change in preview.changes:
        if change.change == "added":
            if preview.exists_now:
                lines.append(f"{change.name} comes back.")
        elif change.change == "removed":
            lines.append(f"{change.name} goes, since the restore point didn't have it.")
        else:
            lines.append(f"{change.name} changes:")
            lines += [f"    {detail}" for detail in change.details]
    if not preview.point.whole:
        lines.append("Only these files change. The profile's other files stay as they are.")
    if preview.missing_mods:
        n = len(preview.missing_mods)
        lines.append(f"{n} mod{' it enables isn' if n == 1 else 's it enables aren'}'t in the instance any more: "
                     f"{conflicts._listing(preview.missing_mods)}. MO2 shows "
                     f"{'it' if n == 1 else 'them'} as missing until you install "
                     f"{'it' if n == 1 else 'them'} again.")
    return lines
