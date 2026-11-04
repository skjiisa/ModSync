"""Daggerfall Unity saves, synced without ever mixing two saves.

DFU keeps each save in ``Saves/SAVE<n>/`` (a dozen files) and finds a save by
its character and name, not by its number: a new save takes the first free
number on *that* machine. Syncing the folder as it is would let two machines
each make a different ``SAVE7``, or each a QuickSave of the same character,
and Syncthing, working file by file, would build one save out of the two.

So the folder that syncs is not ``Saves`` but a *library* ModSync keeps
beside it, where nothing is ever written twice:

``<library>/<key>/<version>/``
    One version of one save, copied whole from a ``SAVE<n>``, with a
    ``modsync-save.json`` manifest (character, name, the file list with sizes
    and checksums). ``<key>`` stands for the character and save name;
    ``<version>`` is the save's own time (``SaveInfo.txt``'s ``realTime``)
    and the machine that made it, so no two machines ever write one path.

``<library>/<key>/<time>-<machine>.deleted``
    The save was deleted at that time on that machine.

Whenever DFU isn't running, :func:`sync` copies new and changed local saves
into the library, then makes each save's newest version (by DFU's own clock)
the one in its local slot: an older local copy is replaced, a save new here
gets a free number, a deleted one goes. The version replaced stays in the
library (the newest few of each save are kept), so the save that lost to a
newer one can still be had. A version still arriving is never used: its
files must match its manifest first.

Which local slot holds which save, as last synced, is in
``Saves/.modsync-saves.json``. DFU only reads ``SAVE<n>`` folders, so that and
ModSync's temporary folders (``.modsync-*``) are invisible to it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None

log = logging.getLogger(__name__)

MAPPING = ".modsync-saves.json"  # in the local Saves folder; never synced
MANIFEST = "modsync-save.json"  # in each version in the library
KEEP_VERSIONS = 3  # of each save, newest first
_SLOT_RE = re.compile(r"^SAVE(\d+)$")
_RECORD_RE = re.compile(r"^(?P<time>\d{20})-(?P<machine>[0-9a-f]{8})(?P<deleted>\.deleted)?$")
_EPOCH = datetime(1, 1, 1)  # .NET's DateTime ticks count 100 ns from here


def ticks(when: datetime | None = None) -> int:
    """Now (or ``when``), local time, the way DFU stamps ``realTime``."""
    when = when or datetime.now()
    delta = when - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


@dataclass(frozen=True)
class Info:
    character: str
    name: str
    real_time: int

    @property
    def key(self) -> str:
        return hashlib.sha256(f"{self.character}\n{self.name}".encode()).hexdigest()[:16]


def read_info(slot: Path) -> Info | None:
    """The character, name and time ``SaveInfo.txt`` gives, or None for a
    folder that isn't a usable save (DFU skips those too)."""
    try:
        data = json.loads((slot / "SaveInfo.txt").read_text(encoding="utf-8-sig"))
        return Info(str(data["characterName"]), str(data["saveName"]), int(data["dateAndTime"]["realTime"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*") if p.is_file() and p.name != MANIFEST)


def fingerprint(slot: Path) -> str:
    """Changes whenever DFU writes the save again (it rewrites every file)."""
    h = hashlib.sha256()
    for p in _files(slot):
        st = p.stat()
        h.update(f"{p.relative_to(slot).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def complete(version: Path) -> bool:
    """Every file the manifest lists is here, whole: the version has fully
    arrived from the machine that made it."""
    try:
        manifest = json.loads((version / MANIFEST).read_text(encoding="utf-8"))
        files = manifest["files"]
        # Sizes first: until the last file has arrived this fails without reading any.
        if not all((version / rel).is_file() and (version / rel).stat().st_size == meta["size"]
                   for rel, meta in files.items()):
            return False
        return all(_sha256(version / rel) == meta["sha256"] for rel, meta in files.items())
    except (OSError, ValueError, KeyError, TypeError):
        return False


@dataclass
class Record:
    path: Path
    time: int
    machine: str
    deleted: bool

    @property
    def order(self) -> tuple[int, str]:
        return self.time, self.machine


def records(key_dir: Path, verified: set[str] | None = None) -> list[Record]:
    """A save's versions and deletions in the library, oldest first. Versions
    still arriving are left out. ``verified`` holds ``<key>/<version>`` of
    versions already found complete: they're written once, so they stay so,
    and checking again would read every save on every sync."""
    out = []
    try:
        entries = list(key_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = _RECORD_RE.match(entry.name)
        if not m:
            continue
        deleted = bool(m["deleted"])
        if deleted:
            if not entry.is_file():
                continue
        else:
            name = f"{key_dir.name}/{entry.name}"
            if not entry.is_dir():
                continue
            if verified is None or name not in verified:
                if not complete(entry):
                    continue
                if verified is not None:
                    verified.add(name)
        out.append(Record(entry, int(m["time"]), m["machine"], deleted))
    return sorted(out, key=lambda r: r.order)


@dataclass
class Report:
    stored: list[str] = field(default_factory=list)  # local saves copied into the library
    placed: list[str] = field(default_factory=list)  # newer versions put into a local slot
    removed: list[str] = field(default_factory=list)  # local slots of saves deleted elsewhere
    deleted: list[str] = field(default_factory=list)  # saves deleted here, recorded for the others
    skipped: str = ""  # why nothing was done

    @property
    def changed(self) -> bool:
        return bool(self.stored or self.placed or self.removed or self.deleted)

    def summary(self) -> str:
        if self.skipped:
            return self.skipped
        parts = [f"{len(v)} {what}" for v, what in ((self.stored, "stored"), (self.placed, "brought in"),
                                                    (self.removed, "removed"), (self.deleted, "deletion(s) shared"))
                 if v]
        return ", ".join(parts) if parts else "Saves are up to date."


def new_machine_id() -> str:
    return secrets.token_hex(4)


def valid_machine_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{8}", value) is not None


class _Mapping:
    def __init__(self, path: Path, machine: str) -> None:
        self.path = path
        self.machine = machine
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        slots = data.get("slots")
        self.slots: dict[str, dict] = slots if isinstance(slots, dict) else {}
        verified = data.get("verified")
        self.verified: set[str] = {v for v in verified if isinstance(v, str)} if isinstance(verified, list) else set()

    def save(self, library: Path) -> None:
        self.verified = {v for v in self.verified if (library / v).is_dir()}  # pruned ones go
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"slots": self.slots, "verified": sorted(self.verified)},
                                  indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


@contextmanager
def _locked(saves: Path):
    """The app, the background service and Steam's Play hook can each sync."""
    if fcntl is None:
        yield
        return
    with open(saves / ".modsync-saves.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _slots(saves: Path) -> tuple[dict[str, tuple[Info, str]], set[str]]:
    """The readable saves here, and every ``SAVE<n>`` folder (readable or not)."""
    out, present = {}, set()
    for entry in saves.iterdir():
        if entry.is_dir() and _SLOT_RE.match(entry.name):
            present.add(entry.name)
            info = read_info(entry)
            if info is not None:
                out[entry.name] = (info, fingerprint(entry))
    return out, present


def _recover(saves: Path, library: Path) -> None:
    """Finish what an interrupted sync left half done (the Play hook's time
    limit, a crash): a slot caught mid-swap gets its old contents back, so it
    never reads as deleted, and leftover copies go."""
    for old in saves.glob(".modsync-old-SAVE*"):
        slot = saves / old.name.removeprefix(".modsync-old-")
        if slot.exists():
            shutil.rmtree(old, ignore_errors=True)  # the swap was done; only the clean-up wasn't
        else:
            os.rename(old, slot)
            log.warning("put back %s after an interrupted sync", slot.name)
    for leftover in [*saves.glob(".modsync-incoming-*"), *library.glob(".incoming-*")]:
        shutil.rmtree(leftover, ignore_errors=True)


def _store(slot: Path, info: Info, machine: str, library: Path) -> Path:
    """Copy a local save into the library as a new version, whole, then move
    it into place, so Syncthing never sends half of one."""
    dest = library / info.key / f"{info.real_time:020d}-{machine}"
    if dest.exists():
        if complete(dest):
            return dest
        shutil.rmtree(dest)  # this machine's own, cut short before its manifest: write it again
    incoming = library / f".incoming-{secrets.token_hex(4)}"
    shutil.copytree(slot, incoming)
    files = {p.relative_to(incoming).as_posix(): {"size": p.stat().st_size, "sha256": _sha256(p)}
             for p in _files(incoming)}
    (incoming / MANIFEST).write_text(json.dumps({"character": info.character, "name": info.name,
                                                 "realTime": info.real_time, "machine": machine,
                                                 "files": files}, indent=2) + "\n", encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(incoming, dest)
    return dest


def _store_and_verify(slot: Path, info: Info, mapping: "_Mapping", library: Path) -> Path:
    version = _store(slot, info, mapping.machine, library)
    mapping.verified.add(f"{info.key}/{version.name}")  # written here, whole
    return version


def _place(version: Path, slot: Path) -> None:
    """Make ``slot`` hold exactly ``version``, swapped in whole."""
    incoming = slot.with_name(f".modsync-incoming-{slot.name}")
    old = slot.with_name(f".modsync-old-{slot.name}")
    for leftover in (incoming, old):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(version, incoming, ignore=shutil.ignore_patterns(MANIFEST, ".syncthing.*", "*.sync-conflict-*"))
    if slot.exists():
        os.rename(slot, old)
    os.rename(incoming, slot)
    shutil.rmtree(old, ignore_errors=True)


def _free_slot(saves: Path) -> Path:
    n = 0
    while (saves / f"SAVE{n}").exists():
        n += 1
    return saves / f"SAVE{n}"


def sync(saves: Path, library: Path, machine: str) -> Report:
    """Bring ``saves`` (DFU's folder) and ``library`` (the synced one) in line.
    ``machine`` names this machine in the versions it writes; it must differ
    from every other machine's. Call only while DFU isn't running: it writes
    saves as it pleases."""
    report = Report()
    if not saves.is_dir():
        report.skipped = f"{saves} doesn't exist yet."
        return report
    library.mkdir(parents=True, exist_ok=True)
    with _locked(saves):
        _recover(saves, library)
        mapping = _Mapping(saves / MAPPING, machine)
        local, present = _slots(saves)

        # 1. What changed here goes into the library.
        for slot, (info, fp) in sorted(local.items()):
            known = mapping.slots.get(slot)
            if known and known.get("fingerprint") == fp:
                continue
            if known and known.get("key") != info.key:
                # The slot now holds another save: the one it held was deleted.
                _delete(library, known, mapping.machine, report)
            version = _store_and_verify(saves / slot, info, mapping, library)
            mapping.slots[slot] = {"key": info.key, "version": version.name, "fingerprint": fp}
            report.stored.append(f"{info.character}: {info.name}")
        # Only a slot that is gone was deleted; one DFU can't read either is left alone.
        for slot in [s for s in mapping.slots if s not in present]:
            _delete(library, mapping.slots.pop(slot), mapping.machine, report)
        unreadable = {mapping.slots[s]["key"] for s in present - set(local) if s in mapping.slots}

        # 2. Each save's newest version is the one here.
        by_key = {m["key"]: slot for slot, m in mapping.slots.items()}
        seen: dict[Path, list[Record]] = {}
        for key_dir in sorted(p for p in library.iterdir() if p.is_dir() and not p.name.startswith(".")):
            found = seen[key_dir] = records(key_dir, mapping.verified)
            if not found or key_dir.name in unreadable:
                continue
            latest = found[-1]
            slot = by_key.get(key_dir.name)
            if latest.deleted:
                if slot is not None:
                    shutil.rmtree(saves / slot)
                    del mapping.slots[slot]
                    report.removed.append(slot)
                continue
            if slot is not None and mapping.slots[slot]["version"] == latest.path.name:
                continue
            target = saves / slot if slot is not None else _free_slot(saves)
            _place(latest.path, target)
            info = read_info(target)
            mapping.slots[target.name] = {"key": key_dir.name, "version": latest.path.name,
                                          "fingerprint": fingerprint(target)}
            report.placed.append(f"{info.character}: {info.name}" if info else target.name)
        # 3. Keep the newest few versions of each save.
        for key_dir, found in seen.items():
            _prune(found)
        mapping.save(library)
    if report.changed:
        log.info("DFU saves: %s", report.summary())
    return report


def _delete(library: Path, known: dict, machine: str, report: Report) -> None:
    """Record that the save ``known`` held was deleted here. When isn't known
    (ModSync may only notice much later), so the marker is dated just after
    the version deleted: a save made anywhere since then wins over it."""
    m = _RECORD_RE.match(str(known.get("version", "")))
    when = int(m["time"]) + 1 if m else ticks()
    marker = library / known["key"] / f"{when:020d}-{machine}.deleted"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    report.deleted.append(known["key"])


def _prune(found: list[Record]) -> None:
    versions = [r for r in found if not r.deleted]
    for old in versions[:-KEEP_VERSIONS]:
        shutil.rmtree(old.path, ignore_errors=True)
    newest = versions[-1].order if versions else None
    deletions = [r for r in found if r.deleted]
    for marker in deletions[:-1]:  # only the latest deletion still says anything
        marker.path.unlink(missing_ok=True)
    if deletions and newest is not None and deletions[-1].order < newest:
        deletions[-1].path.unlink(missing_ok=True)  # the save came back after it
