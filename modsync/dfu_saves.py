"""Daggerfall Unity saves, synced without ever mixing two saves.

DFU keeps each save in ``Saves/SAVE<n>/`` (a dozen files) and finds a save by
its character and name, not by its number: a new save takes the first free
number on *that* machine. Syncing the folder as it is would let two machines
each make a different ``SAVE7``, or each a QuickSave of the same character,
and Syncthing, working file by file, would build one save out of the two.

So the folder that syncs is not ``Saves`` but a *library* ModSync keeps
beside it, where nothing is ever written twice:

``<library>/<key>/<time>-<machine>.id``
    That a version of a save exists: its *version vector* and a digest of its
    files. ``<key>`` stands for the character and save name; ``<time>`` is the
    save's own (``SaveInfo.txt``'s ``realTime``) and ``<machine>`` the one that
    made it, so no two machines ever write one path. Kept for good (a few
    bytes): which version wins is decided from these alone, so removing old
    versions' files can never change it, and a local save whose files match
    one, however old, is known to be that version.

``<library>/<key>/<time>-<machine>/``
    That version's files, copied whole from a ``SAVE<n>``, with a
    ``modsync-save.json`` manifest (the file list with sizes and checksums).
    Old ones are removed: the winner and the newest few others stay.

``<library>/<key>/<time>-<machine>.deleted``
    The save was deleted on that machine, with the version vector of the
    deletion. These are kept for good: a few bytes each, and removing one
    could bring back what it deleted.

A version vector counts, per machine, the changes to the save a version
includes. A machine that makes a version takes the vector of the version its
slot held and adds one for itself, so one version was made from another
exactly when its vector is at least the other's everywhere. That stays true
whatever is removed and however long the history. A version made from
another always replaces it, whatever the clocks of the machines say (DFU's
times are local time, so time zones and the end of summer time would
otherwise let older progress win). Only versions made separately (each
machine played on before they met) are decided by DFU's clock, and both
are kept. A slot is never moved to a version its own includes.

Whenever DFU isn't running, :func:`sync` copies new and changed local saves
into the library, then makes each save's winning version the one in its
local slot: an older local copy is replaced (swapped in one step where the
filesystem allows, so an interrupted sync never leaves a slot missing), a
save new here gets a free number, a deleted one goes (moved out of DFU's
sight in one step first, so half of it is never seen as a save). A local
save whose files match a version already in the library is that version:
nothing that only lost track of where it came from is ever published anew.
A version is used only once its files match its manifest, checked again as
it goes into a slot; until the winner's files have arrived, its save is left
as it is here.

Which local slot holds which version, as last synced with one library, is in
``Saves/.modsync-saves-<library>.json``: the Flatpak and a native ModSync on
one machine keep separate libraries and must not take each other's word.
DFU only reads ``SAVE<n>`` folders, so that and ModSync's temporary folders
(``.modsync-*``) are invisible to it.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
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
from typing import Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None

log = logging.getLogger(__name__)

MAPPING_PREFIX = ".modsync-saves-"  # + the library's id + ".json", in the local Saves folder; never synced
MANIFEST = "modsync-save.json"  # in each version in the library
KEEP_VERSIONS = 3  # other versions of each save kept besides the winner, newest first
_SLOT_RE = re.compile(r"^SAVE(\d+)$")
_MACHINE_RE = re.compile(r"^[0-9a-f]{8}$")
_RECORD_RE = re.compile(r"^(?P<time>\d{20})-(?P<machine>[0-9a-f]{8})(?P<deleted>\.deleted)?$")
_EPOCH = datetime(1, 1, 1)  # .NET's DateTime ticks count 100 ns from here

VV = dict[str, int]


def ticks(when: datetime | None = None) -> int:
    """Now (or ``when``), local time, the way DFU stamps ``realTime``."""
    when = when or datetime.now()
    delta = when - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 10_000_000 + delta.microseconds * 10


def _vv(value: object) -> VV:
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and _MACHINE_RE.match(k)
            and isinstance(v, int) and not isinstance(v, bool) and v > 0}


def included(a: VV, b: VV) -> bool:
    """Every change ``a`` counts, ``b`` counts too: ``b`` is ``a`` or was made from it."""
    return all(b.get(m, 0) >= n for m, n in a.items())


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


def _hashes(folder: Path) -> dict[str, dict]:
    return {p.relative_to(folder).as_posix(): {"size": p.stat().st_size, "sha256": _sha256(p)} for p in _files(folder)}


def _manifest(version: Path) -> dict | None:
    try:
        data = json.loads((version / MANIFEST).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("files"), dict) else None
    except (OSError, ValueError):
        return None


def matches(folder: Path, files: dict) -> bool:
    """``folder`` holds exactly what ``files`` (a manifest's) lists, whole."""
    try:
        if len(_files(folder)) != len(files):
            return False
        # Sizes first: until the last file has arrived this fails without reading any.
        if not all((folder / rel).is_file() and (folder / rel).stat().st_size == meta["size"]
                   for rel, meta in files.items()):
            return False
        return all(_sha256(folder / rel) == meta["sha256"] for rel, meta in files.items())
    except (OSError, KeyError, TypeError):
        return False


def complete(version: Path) -> bool:
    """Every file the manifest lists is here, whole: the version has fully
    arrived from the machine that made it (or not yet begun to go)."""
    manifest = _manifest(version)
    return manifest is not None and matches(version, manifest["files"])


@dataclass
class Record:
    path: Path  # the version's files (may be gone, or not have arrived yet), or the deletion marker
    time: int
    machine: str
    deleted: bool
    vv: VV = field(default_factory=dict)
    digest: str = ""  # of the version's files, from its .id

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def order(self) -> tuple[int, str]:
        return self.time, self.machine


def digest(files: dict) -> str:
    """One checksum for a version's whole file list (as its manifest has it)."""
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def records(key_dir: Path) -> list[Record]:
    """A save's versions (from their ``.id`` files, whether or not their
    files are here) and deletions, oldest first. Records without a version
    vector are left out."""
    out = []
    try:
        entries = list(key_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.suffix == ".id":
            m = _RECORD_RE.match(entry.stem)
            if not m or m["deleted"] or not entry.is_file():
                continue
            data = _read_json(entry)
            vv, files_digest = _vv(data.get("vv")), data.get("digest")
            if vv and isinstance(files_digest, str):
                out.append(Record(entry.with_suffix(""), int(m["time"]), m["machine"], False, vv, files_digest))
            continue
        m = _RECORD_RE.match(entry.name)
        if m and m["deleted"] and entry.is_file():
            vv = _vv(_read_json(entry).get("vv"))
            if vv:
                out.append(Record(entry, int(m["time"]), m["machine"], True, vv))
    return sorted(out, key=lambda r: r.order)


def available(record: Record, verified: set[str] | None = None) -> bool:
    """The version's files are all here, whole, and they are the ones its
    ``.id`` names. ``verified`` holds ``<key>/<version>`` of versions already
    found so: written once, they stay so (until removed, which ``_place``
    notices)."""
    name = f"{record.path.parent.name}/{record.name}"
    if verified is not None and name in verified:
        return record.path.is_dir()
    manifest = _manifest(record.path)
    ok = manifest is not None and digest(manifest["files"]) == record.digest and complete(record.path)
    if ok and verified is not None:
        verified.add(name)
    return ok


def heads(found: list[Record]) -> list[Record]:
    """The records nothing was made from, oldest first. Two with the same
    vector (which shouldn't happen) are both kept, and the newer wins."""
    return [r for r in found if not any(o is not r and o.vv != r.vv and included(r.vv, o.vv) for o in found)]


def winner(found: list[Record]) -> Record | None:
    """The record that decides a save: of those nothing was made from, the
    newest by DFU's clock (only versions made separately are compared so)."""
    tops = heads(found)
    return tops[-1] if tops else None


@dataclass
class Report:
    stored: list[str] = field(default_factory=list)  # local saves copied into the library
    placed: list[str] = field(default_factory=list)  # winning versions put into a local slot
    removed: list[str] = field(default_factory=list)  # local slots of saves deleted elsewhere
    deleted: list[str] = field(default_factory=list)  # saves deleted here, recorded for the others
    skipped: str = ""  # why nothing (more) was done

    @property
    def changed(self) -> bool:
        return bool(self.stored or self.placed or self.removed or self.deleted)

    def summary(self) -> str:
        parts = [f"{len(v)} {what}" for v, what in ((self.stored, "stored"), (self.placed, "brought in"),
                                                    (self.removed, "removed"), (self.deleted, "deletion(s) shared"))
                 if v]
        if self.skipped:
            return "; ".join([*parts, self.skipped]) if parts else self.skipped
        return ", ".join(parts) if parts else "Saves are up to date."


class Machine:
    """This machine in the versions it writes: an id no other machine has,
    and the count of changes it has made (the vectors' own entry)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        ident, counter = data.get("id"), data.get("counter")
        self.id = ident if isinstance(ident, str) and _MACHINE_RE.match(ident) else secrets.token_hex(4)
        self.counter = counter if isinstance(counter, int) and counter >= 0 else 0
        if ident != self.id:
            self._save()

    def next(self, seen: int = 0) -> int:
        """A count this machine never gave before: past any in its file (read
        again under a lock, since the app, the background service and the
        Play hook each have one of these) and past ``seen``, the highest
        found in the library and in what the slot held, in case the file was
        lost."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a") as lock:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = _read_json(self.path)
                stored = data.get("counter") if data.get("id") == self.id else None
                self.counter = max(self.counter, stored if isinstance(stored, int) else 0, seen) + 1
                self._save()
            finally:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        return self.counter

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"id": self.id, "counter": self.counter}) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def mapping_path(saves: Path, library: Path) -> Path:
    library_id = hashlib.sha256(str(library.resolve()).encode()).hexdigest()[:12]
    return saves / f"{MAPPING_PREFIX}{library_id}.json"


class _Mapping:
    def __init__(self, path: Path, library: Path) -> None:
        self.path = path
        self.library = library
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        slots = data.get("slots") if isinstance(data, dict) else None
        self.slots: dict[str, dict] = {s: {**e, "vv": _vv(e.get("vv"))} for s, e in slots.items()
                                       if isinstance(e, dict) and isinstance(e.get("key"), str)} \
            if isinstance(slots, dict) else {}
        verified = data.get("verified") if isinstance(data, dict) else None
        self.verified: set[str] = {v for v in verified if isinstance(v, str)} if isinstance(verified, list) else set()

    def save(self) -> None:
        """After every change to a slot, so an interrupted sync loses none of them."""
        self.verified = {v for v in self.verified if (self.library / v).is_dir()}  # removed ones go
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"library": str(self.library), "slots": self.slots,
                                   "verified": sorted(self.verified)}, indent=2) + "\n", encoding="utf-8")
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
    """Finish what an interrupted sync left half done: a slot caught mid-swap
    (where the swap couldn't be one step) gets its old contents back, so it
    never reads as deleted; a slot being removed and leftover copies go."""
    for old in saves.glob(".modsync-old-SAVE*"):
        slot = saves / old.name.removeprefix(".modsync-old-")
        if slot.exists():
            shutil.rmtree(old, ignore_errors=True)  # the swap was done; only the clean-up wasn't
        else:
            os.rename(old, slot)
            log.warning("put back %s after an interrupted sync", slot.name)
    for leftover in [*saves.glob(".modsync-incoming-*"), *saves.glob(".modsync-trash-*"), *library.glob(".incoming-*")]:
        shutil.rmtree(leftover, ignore_errors=True)


_RENAME_EXCHANGE = 2
_AT_FDCWD = -100
_libc = None


def _exchange(a: Path, b: Path) -> bool:
    """Swap two folders in one step (Linux's renameat2 RENAME_EXCHANGE).
    False where that isn't available: an old kernel or C library, or a
    filesystem without it (some FUSE ones, exFAT on older kernels)."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
            _libc.renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            _libc.renameat2.restype = ctypes.c_int
        result = _libc.renameat2(_AT_FDCWD, os.fsencode(a), _AT_FDCWD, os.fsencode(b), _RENAME_EXCHANGE)
    except (OSError, AttributeError):
        return False
    if result != 0:
        err = ctypes.get_errno()
        if err in (22, 38, 95):  # EINVAL, ENOSYS, EOPNOTSUPP
            return False
        raise OSError(err, os.strerror(err), str(a))
    return True


def _write_once(path: Path, data: dict) -> None:
    tmp = path.with_name(f".incoming-{secrets.token_hex(4)}")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.rename(tmp, path)


def _store(slot: Path, info: Info, machine: str, library: Path, vv: VV) -> Path:
    """Copy a local save into the library as a new version, whole, then move
    it into place, so Syncthing never sends half of one; its ``.id`` last, so
    it never names files that aren't there."""
    dest = library / info.key / f"{info.real_time:020d}-{machine}"
    ident = dest.with_name(dest.name + ".id")
    if dest.exists():
        shutil.rmtree(dest)  # this machine's own, cut short before its .id
    incoming = library / f".incoming-{secrets.token_hex(4)}"
    shutil.copytree(slot, incoming)
    files = _hashes(incoming)
    (incoming / MANIFEST).write_text(json.dumps({"character": info.character, "name": info.name,
                                                 "realTime": info.real_time, "machine": machine,
                                                 "files": files}, indent=2) + "\n", encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(incoming, dest)
    _write_once(ident, {"character": info.character, "name": info.name, "vv": vv, "digest": digest(files)})
    return dest


def _same_as(slot: Path, found: list[Record]) -> Record | None:
    """The library version ``slot`` holds file for file, if any, even one
    whose files have since been removed: a slot that lost track of where it
    came from (its mapping gone, a sync cut short between placing it and
    noting so) is that version, not a new one."""
    wanted = digest(_hashes(slot))
    return next((r for r in reversed(found) if not r.deleted and r.digest == wanted), None)


def _place(version: Record, slot: Path) -> bool:
    """Make ``slot`` hold exactly ``version``. The copy is checked against the
    manifest first (the version may have changed since it was found whole:
    another machine pruning it, say), and swapped in in one step where the
    filesystem allows. False, changing nothing, when the copy isn't whole."""
    manifest = _manifest(version.path)
    if manifest is None or digest(manifest["files"]) != version.digest:
        return False
    incoming = slot.with_name(f".modsync-incoming-{slot.name}")
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.copytree(version.path, incoming,
                    ignore=shutil.ignore_patterns(MANIFEST, ".syncthing.*", "*.sync-conflict-*"))
    if not matches(incoming, manifest["files"]):
        shutil.rmtree(incoming, ignore_errors=True)
        return False
    if not slot.exists():
        os.rename(incoming, slot)
        return True
    if _exchange(incoming, slot):
        shutil.rmtree(incoming, ignore_errors=True)  # now the old contents
        return True
    old = slot.with_name(f".modsync-old-{slot.name}")
    shutil.rmtree(old, ignore_errors=True)
    os.rename(slot, old)
    os.rename(incoming, slot)
    shutil.rmtree(old, ignore_errors=True)
    return True


def _remove(slot: Path) -> None:
    """Take a slot away: out of DFU's sight in one step, then deleted."""
    trash = slot.with_name(f".modsync-trash-{slot.name}")
    shutil.rmtree(trash, ignore_errors=True)
    os.rename(slot, trash)
    shutil.rmtree(trash, ignore_errors=True)


def _free_slot(saves: Path) -> Path:
    n = 0
    while (saves / f"SAVE{n}").exists():
        n += 1
    return saves / f"SAVE{n}"


def _key_dirs(library: Path) -> list[Path]:
    return sorted(p for p in library.iterdir() if p.is_dir() and not p.name.startswith("."))


Guard = Callable[[], "contextlib.AbstractContextManager[str]"]


@contextmanager
def _always_free():
    yield ""


def sync(saves: Path, library: Path, machine: Machine, *, guard: Guard | None = None) -> Report:
    """Bring ``saves`` (DFU's folder) and ``library`` (the synced one) in line.

    ``guard()`` is entered around each change to a slot here, and once at
    the start: it yields why DFU may be writing saves right now ("" when
    not), and while entered keeps DFU from starting (Steam's Play hook
    waits for it). From the moment it gives a reason, nothing more is
    changed."""
    report = Report()
    if not saves.is_dir():
        report.skipped = f"{saves} doesn't exist yet."
        return report
    library.mkdir(parents=True, exist_ok=True)
    guard = guard or _always_free

    with _locked(saves):
        with guard() as reason:
            if reason:
                report.skipped = reason
                return report
            _recover(saves, library)
        mapping = _Mapping(mapping_path(saves, library), library)
        found = {d.name: records(d) for d in _key_dirs(library)}
        mine = max((r.vv.get(machine.id, 0) for rs in found.values() for r in rs), default=0)

        def bumped(vv: VV) -> VV:
            return {**vv, machine.id: machine.next(max(mine, vv.get(machine.id, 0)))}

        local, present = _slots(saves)

        # 1. What changed here goes into the library.
        for slot, (info, fp) in sorted(local.items()):
            known = mapping.slots.get(slot)
            if known and known.get("fingerprint") == fp:
                continue
            with guard() as reason:
                if reason:
                    report.skipped = reason
                    return report
                same = known is not None and known["key"] == info.key
                if known and not same:
                    # The slot now holds another save: the one it held was deleted.
                    # Recorded before the slot's new entry, so it can't be lost to an interruption.
                    _delete(library, known, bumped(known["vv"]), machine.id, report)
                twin = _same_as(saves / slot, found.get(info.key, []))
                if twin is not None:  # nothing new: it is that version
                    mapping.slots[slot] = {"key": info.key, "version": twin.name, "fingerprint": fp, "vv": twin.vv}
                    mapping.save()
                    continue
                vv = bumped(known["vv"] if same else {})
                version = _store(saves / slot, info, machine.id, library, vv)
                mapping.verified.add(f"{info.key}/{version.name}")
                mapping.slots[slot] = {"key": info.key, "version": version.name, "fingerprint": fp, "vv": vv}
                mapping.save()
                report.stored.append(f"{info.character}: {info.name}")
        # Only a slot that is gone was deleted; one DFU can't read either is left alone.
        for slot in [s for s in mapping.slots if s not in present]:
            known = mapping.slots[slot]
            _delete(library, known, bumped(known["vv"]), machine.id, report)
            del mapping.slots[slot]
            mapping.save()
        unreadable = {mapping.slots[s]["key"] for s in present - set(local) if s in mapping.slots}

        # 2. Each save's winning version is the one here.
        by_key = {m["key"]: slot for slot, m in mapping.slots.items()}
        for key_dir in _key_dirs(library):
            found[key_dir.name] = recs = records(key_dir)
            best = winner(recs)
            if best is None or key_dir.name in unreadable:
                continue
            slot = by_key.get(key_dir.name)
            held = mapping.slots[slot] if slot is not None else None
            if held is not None and (best.name == held["version"] or
                                     (best.vv != held["vv"] and included(best.vv, held["vv"]))):
                continue  # it holds that version, or one made from it: never go back
            if best.deleted and slot is None:
                continue
            if not best.deleted and not available(best, mapping.verified):
                continue  # the winner's files haven't all arrived: leave this save as it is for now
            with guard() as reason:
                if reason:
                    report.skipped = reason
                    return report
                if best.deleted:
                    _remove(saves / slot)
                    del mapping.slots[slot]
                    mapping.save()
                    report.removed.append(slot)
                    continue
                target = saves / slot if slot is not None else _free_slot(saves)
                if not _place(best, target):
                    mapping.verified.discard(f"{key_dir.name}/{best.name}")  # check it again next time
                    continue
                info = read_info(target)
                mapping.slots[target.name] = {"key": key_dir.name, "version": best.name,
                                              "fingerprint": fingerprint(target), "vv": best.vv}
                mapping.save()
                report.placed.append(f"{info.character}: {info.name}" if info else target.name)

        # 3. Remove old versions' files (never their .id), but the newest few.
        for recs in found.values():
            _prune(recs)
        mapping.save()
    if report.changed:
        log.info("DFU saves: %s", report.summary())
    return report


def _delete(library: Path, known: dict, vv: VV, machine: str, report: Report) -> None:
    """Record that the save ``known`` held was deleted here. ``vv`` (the
    version's vector, one more for this machine) makes the deletion replace
    that version and all it was made from. A version made separately
    elsewhere is weighed against it by time, so the marker is dated just
    after the version deleted (when it was deleted isn't known; ModSync may
    only notice much later) and any save made since wins."""
    m = _RECORD_RE.match(str(known.get("version", "")))
    when = int(m["time"]) + 1 if m else ticks()
    marker = library / known["key"] / f"{when:020d}-{machine}.deleted"
    report.deleted.append(known["key"])
    if marker.exists():
        return  # recorded already, by a sync cut short before it noted so
    marker.parent.mkdir(parents=True, exist_ok=True)
    _write_once(marker, {"vv": vv})


def _prune(found: list[Record]) -> None:
    """Remove the files of versions something was made from, but the newest
    few of them. Their ``.id`` files and every deletion stay, so what wins
    never changes for it."""
    tops = {id(r) for r in heads(found)}
    replaced = [r for r in found if not r.deleted and id(r) not in tops and r.path.is_dir()]
    for old in replaced[:-KEEP_VERSIONS]:
        shutil.rmtree(old.path, ignore_errors=True)
