"""Daggerfall Unity saves, synced without ever mixing two saves.

DFU keeps each save in ``Saves/SAVE<n>/`` (a dozen files) and finds a save by
its character and name, not by its number: a new save takes the first free
number on *that* machine. Syncing the folder as it is would let two machines
each make a different ``SAVE7``, or each a QuickSave of the same character,
and Syncthing, working file by file, would build one save out of the two.

So the folder that syncs is not ``Saves`` but a *library* ModSync keeps
beside it, where nothing is ever written twice. Per save (``<key>`` stands for
its character and name), each record is named ``<time>-<machine>-<count>``:
the save's own time (``SaveInfo.txt``'s ``realTime``), the machine that made
the record and that machine's count of changes to the save, so no two
records ever share a name.

``<key>/<name>.id``
    That a version exists: its *version vector* and a digest of its files.
    Kept for good (a few bytes): which version wins is decided from these
    alone, so removing old versions' files never changes it, and a local save
    whose files match one, however old, is known to be that version.

``<key>/<name>/``
    That version's files, copied whole from a ``SAVE<n>``, with a
    ``modsync-save.json`` manifest (sizes and checksums). Old ones are
    removed: the ones that could still win and the newest few others stay.

``<key>/<name>.deleted``
    The save was deleted, with the deletion's version vector. Kept for good.

A version vector counts, per machine, the changes to the save that a record
takes in. A slot's own vector is everything it takes in: the version it holds
and, once it took a winner, every version that winner beat. A machine making a
version adds one for itself to its slot's vector, so one record was made after
another exactly when its vector is at least the other's everywhere, however
long the history and whatever is removed. A record made after another always
replaces it, whatever the clocks say (DFU's times are local time, so time
zones and the end of summer time would otherwise let older progress win).
Only records made separately (each machine played on before they met) are
decided by DFU's clock, and both are kept. A slot is never moved to a record
its vector already takes in.

Whenever DFU isn't running, :func:`sync` copies new and changed local saves
into the library, then makes each save's winner the one in its local slot:
an older local copy is replaced, a save new here gets a free number, a
deleted one goes. A slot is replaced in one step where the filesystem allows,
and what it's about to hold is written down first, with the old contents kept
until that's done: a sync cut short at any point is finished or undone by the
next, even after DFU has written to the slot in between. A slot is removed by
moving it out of DFU's sight in one step first. A local save whose files
match a known version is that version, never published anew; a save is
deleted only once no slot here still holds it. A version is used only once
its files match its manifest, checked again as it goes into a slot; until the
winner's files have arrived, its save stays as it is here.

Which local slot holds what, as last synced, is in
``Saves/.modsync-saves-<library>.json``; ``Saves/.modsync-saves-owner`` names
the one library a Saves folder syncs with (the Flatpak and a native ModSync
on one machine each have their own). DFU only reads ``SAVE<n>`` folders, so
those and ModSync's temporary folders (``.modsync-*``) are invisible to it.
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
OWNER = ".modsync-saves-owner"  # in the local Saves folder: the library it syncs with
MANIFEST = "modsync-save.json"  # in each version's folder in the library
KEEP_VERSIONS = 3  # versions' files kept besides those that could still win, newest first
_SLOT_RE = re.compile(r"^SAVE(\d+)$")
_MACHINE_RE = re.compile(r"^[0-9a-f]{8}$")
_NAME_RE = re.compile(r"^(?P<time>\d{20})-(?P<machine>[0-9a-f]{8})-(?P<count>\d{8})$")
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
    """Every change ``a`` counts, ``b`` counts too."""
    return all(b.get(m, 0) >= n for m, n in a.items())


def joined(*vvs: VV) -> VV:
    out: VV = {}
    for vv in vvs:
        for m, n in vv.items():
            out[m] = max(out.get(m, 0), n)
    return out


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


def digest(files: dict) -> str:
    """One checksum for a version's whole file list (as its manifest has it)."""
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def content(folder: Path) -> str:
    return digest(_hashes(folder))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _manifest(version: Path) -> dict | None:
    data = _read_json(version / MANIFEST)
    return data if isinstance(data.get("files"), dict) else None


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


@dataclass
class Record:
    path: Path  # the version's folder (which may be gone, or not here yet), or the deletion
    time: int
    machine: str
    deleted: bool
    vv: VV = field(default_factory=dict)
    digest: str = ""  # of the version's files

    @property
    def name(self) -> str:
        return self.path.name.removesuffix(".deleted")

    @property
    def order(self) -> tuple[int, str, str]:
        return self.time, self.machine, self.name


def records(key_dir: Path, cache: dict[str, dict] | None = None) -> list[Record]:
    """A save's versions (from their ``.id``, whether their files are here or
    not) and deletions, oldest first. ``cache`` keeps what each record says
    (``<key>/<file>`` to its contents): they're never rewritten, so each is
    read once. Records without a version vector are left out."""
    out = []
    try:
        entries = [e.name for e in key_dir.iterdir()]
    except OSError:
        return []
    for entry in entries:
        stem, dot, kind = entry.rpartition(".")
        if not dot or kind not in ("id", "deleted"):
            continue
        m = _NAME_RE.match(stem)
        if not m:
            continue
        where = f"{key_dir.name}/{entry}"
        data = cache.get(where) if cache is not None else None
        if data is None:
            data = _read_json(key_dir / entry)
            if cache is not None and data:
                cache[where] = data
        vv = _vv(data.get("vv"))
        if not vv:
            continue
        if kind == "id":
            if isinstance(data.get("digest"), str):
                out.append(Record(key_dir / stem, int(m["time"]), m["machine"], False, vv, data["digest"]))
        else:
            out.append(Record(key_dir / entry, int(m["time"]), m["machine"], True, vv))
    return sorted(out, key=lambda r: r.order)


def heads(found: list[Record]) -> list[Record]:
    """The records nothing was made after, oldest first. A record can only be
    taken in by one counting at least as many changes, and then by one of
    these: so each is weighed against these alone, from the most changes
    down. Two with the same vector (which shouldn't happen) both stay."""
    tops: list[Record] = []
    for r in sorted(found, key=lambda r: sum(r.vv.values()), reverse=True):
        if not any(t.vv != r.vv and included(r.vv, t.vv) for t in tops):
            tops.append(r)
    return sorted(tops, key=lambda r: r.order)


def winner(found: list[Record]) -> Record | None:
    """The record that decides a save: of those nothing was made after, the
    newest by DFU's clock (only records made separately are compared so)."""
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
    """This machine in the records it writes: an id no other machine has,
    and its count of changes (the vectors' own entry)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        data = _read_json(path)
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
    """Per slot: ``key``, ``version`` (the record it holds) and ``held`` (that
    record's vector), ``vv`` (all the slot takes in: that, and every record it
    settled a conflict against; what a version made from it starts from),
    ``fingerprint`` (of its files when last noted), and while a placement is
    under way ``pending``: what it's about to hold."""

    def __init__(self, path: Path, library: Path) -> None:
        self.path = path
        self.library = library
        data = _read_json(path)
        slots = data.get("slots")
        self.slots: dict[str, dict] = {}
        if isinstance(slots, dict):
            for s, e in slots.items():
                if isinstance(e, dict) and isinstance(e.get("key"), str) and _SLOT_RE.match(s):
                    # A missing "held" is looked up from the record, never guessed (see sync).
                    self.slots[s] = {**e, "vv": _vv(e.get("vv")), "held": _vv(e.get("held"))}
        cache = data.get("records")
        self.cache: dict[str, dict] = {k: v for k, v in cache.items() if isinstance(v, dict)} \
            if isinstance(cache, dict) else {}
        verified = data.get("verified")
        self.verified: set[str] = {v for v in verified if isinstance(v, str)} if isinstance(verified, list) else set()

    def save(self) -> None:
        """After every change to a slot, so an interrupted sync loses none of them."""
        self.verified = {v for v in self.verified if (self.library / v).is_dir()}  # removed ones go
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"library": str(self.library), "slots": self.slots, "records": self.cache,
                                   "verified": sorted(self.verified)}, indent=1) + "\n", encoding="utf-8")
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
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    os.rename(tmp, path)


def _record_name(time: int, machine: str, count: int) -> str:
    return f"{time:020d}-{machine}-{count:08d}"


def _store(slot: Path, info: Info, machine: str, count: int, library: Path, vv: VV) -> str:
    """Copy a local save into the library as a new version, whole: its folder
    first, moved into place in one step, then its ``.id``, so a record never
    names files that weren't there."""
    name = _record_name(info.real_time, machine, count)
    dest = library / info.key / name
    incoming = library / f".incoming-{secrets.token_hex(4)}"
    shutil.copytree(slot, incoming)
    files = _hashes(incoming)
    (incoming / MANIFEST).write_text(json.dumps({"character": info.character, "name": info.name,
                                                 "realTime": info.real_time, "files": files}, indent=1) + "\n",
                                     encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(incoming, dest)
    _write_once(dest.with_name(name + ".id"), {"character": info.character, "name": info.name,
                                               "vv": vv, "digest": digest(files)})
    return name


def _available(record: Record, verified: set[str]) -> bool:
    """The version's files are all here, whole, and they are the ones its
    ``.id`` names. Once found so they stay so (written once), until removed,
    which placing it notices."""
    where = f"{record.path.parent.name}/{record.name}"
    if where in verified:
        return record.path.is_dir()
    manifest = _manifest(record.path)
    ok = manifest is not None and digest(manifest["files"]) == record.digest and matches(record.path, manifest["files"])
    if ok:
        verified.add(where)
    return ok


def _incoming(slot: Path) -> Path:
    return slot.with_name(f".modsync-incoming-{slot.name}")


def _old(slot: Path) -> Path:
    return slot.with_name(f".modsync-old-{slot.name}")


def _settle_placement(saves: Path, slot: str, entry: dict) -> tuple[dict | None, list[Path]]:
    """Finish or undo a placement a sync was cut short in. ``pending`` is
    written only once the copy beside the slot (``.modsync-incoming-``) is
    whole and checked, so from what the slot and its copies hold now the
    swap can be told apart: that copy still there with the new files, not
    swapped; there with other files, swapped in one step (it holds the old
    ones); gone, moved into place. Returns the slot's entry as it now stands
    (None: the slot holds nothing of ours), and the copies to remove once
    that is saved: removed first, a recovery cut short could no longer tell."""
    pending = entry["pending"]
    entry = {k: v for k, v in entry.items() if k != "pending"}
    target, incoming, old = saves / slot, _incoming(saves / slot), _old(saves / slot)
    if old.exists() and not target.exists():
        os.rename(old, target)  # cut short between the two renames of a two-step swap: undo it
        swapped = False
    elif incoming.exists():
        swapped = content(incoming) != pending["digest"]
    else:
        swapped = target.exists()
    leftovers = [incoming, old]
    if not swapped:
        return (entry if entry.get("version") else None), leftovers
    # It holds the placed version, or (if DFU saved since) one made from it:
    # "" as fingerprint has the next step look at it as a change made here.
    still = content(target) == pending["digest"]
    return ({**_carried(entry), "key": entry["key"], "version": pending["version"], "vv": _vv(pending["vv"]),
             "held": _vv(pending.get("held")), "fingerprint": fingerprint(target) if still else ""}, leftovers)


def _place(record: Record, target: Path, mapping: _Mapping, slot_entry: dict) -> bool:
    """Make ``target`` hold exactly ``record``'s files: copied beside it and
    checked against the manifest, then noted in the mapping as under way,
    swapped in (in one step where the filesystem allows) and noted as done,
    the old contents removed last. False, changing nothing, when the copy
    isn't whole."""
    manifest = _manifest(record.path)
    if manifest is None or digest(manifest["files"]) != record.digest:
        return False
    slot, settled = target.name, slot_entry.pop("vv_new")
    incoming = _incoming(target)
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.copytree(record.path, incoming,
                    ignore=shutil.ignore_patterns(MANIFEST, ".syncthing.*", "*.sync-conflict-*"))
    if not matches(incoming, manifest["files"]):
        shutil.rmtree(incoming, ignore_errors=True)
        return False
    mapping.slots[slot] = {**slot_entry, "pending": {"version": record.name, "vv": settled, "held": record.vv,
                                                     "digest": record.digest}}
    mapping.save()
    if not target.exists():
        os.rename(incoming, target)
    elif not _exchange(incoming, target):
        old = _old(target)
        shutil.rmtree(old, ignore_errors=True)
        os.rename(target, old)
        os.rename(incoming, target)
        incoming = old
    mapping.slots[slot] = {**_carried(slot_entry), "key": slot_entry["key"], "version": record.name,
                           "vv": settled, "held": record.vv, "fingerprint": fingerprint(target)}
    mapping.save()
    shutil.rmtree(incoming, ignore_errors=True)  # the old contents
    return True


def _carried(entry: dict | None) -> dict:
    """What a slot's entry keeps when it gets a new version: for a save with
    copies in several slots, what deleting the others would delete."""
    return {k: entry[k] for k in ("covers", "latest") if entry and k in entry}


def _remove(slot: Path) -> None:
    """Take a slot away: out of DFU's sight in one step, then deleted."""
    trash = slot.with_name(f".modsync-trash-{slot.name}")
    shutil.rmtree(trash, ignore_errors=True)
    os.rename(slot, trash)
    shutil.rmtree(trash, ignore_errors=True)


def _free_slot(saves: Path, mapping: _Mapping) -> Path:
    n = 0
    while (saves / f"SAVE{n}").exists() or f"SAVE{n}" in mapping.slots:
        n += 1
    return saves / f"SAVE{n}"


def _key_dirs(library: Path) -> list[Path]:
    return sorted(p for p in library.iterdir() if p.is_dir() and not p.name.startswith("."))


def _recover(saves: Path, library: Path, mapping: _Mapping, machine: str) -> None:
    """Finish what an interrupted sync left: placements, removals, and this
    machine's own versions cut short before their ``.id``."""
    for slot in [s for s, e in mapping.slots.items() if e.get("pending")]:
        entry, leftovers = _settle_placement(saves, slot, mapping.slots[slot])
        if entry is None:
            del mapping.slots[slot]
        else:
            mapping.slots[slot] = entry
        mapping.save()
        for leftover in leftovers:
            shutil.rmtree(leftover, ignore_errors=True)
    for old in saves.glob(".modsync-old-SAVE*"):
        slot = saves / old.name.removeprefix(".modsync-old-")
        if not slot.exists():
            os.rename(old, slot)  # caught between the two renames of a swap: what it held goes back
            log.warning("put back %s after an interrupted sync", slot.name)
    for leftover in [*saves.glob(".modsync-incoming-*"), *saves.glob(".modsync-trash-*"),
                     *saves.glob(".modsync-old-*"), *library.glob(".incoming-*"), *library.glob("*/.incoming-*")]:
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
        else:
            leftover.unlink(missing_ok=True)
    for key_dir in _key_dirs(library):
        for folder in key_dir.iterdir():
            m = _NAME_RE.match(folder.name)
            if m and m["machine"] == machine and folder.is_dir() and not folder.with_name(folder.name + ".id").exists():
                shutil.rmtree(folder, ignore_errors=True)  # never published


Guard = Callable[[], "contextlib.AbstractContextManager[str]"]


@contextmanager
def _always_free():
    yield ""


def owner_problem(saves: Path, library: Path) -> str:
    """Why ``saves`` can't sync with ``library``: it syncs with another one."""
    owner = saves / OWNER
    try:
        other = owner.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if other and Path(other).resolve() != library.resolve() and Path(other).is_dir():
        return (f"These saves already sync through another ModSync (its library is {other}). "
                f"Stop syncing saves there first, or delete {owner} if that ModSync is gone.")
    return ""


def release(saves: Path, library: Path) -> None:
    """Saves sync stopped here: another ModSync may take these saves over."""
    owner = saves / OWNER
    with _locked(saves) if saves.is_dir() else contextlib.nullcontext():
        try:
            if Path(owner.read_text(encoding="utf-8").strip()).resolve() == library.resolve():
                owner.unlink()
        except OSError:
            pass


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
        report.skipped = owner_problem(saves, library)
        if report.skipped:
            return report
        mapping = _Mapping(mapping_path(saves, library), library)
        with guard() as reason:
            if reason:
                report.skipped = reason
                return report
            (saves / OWNER).write_text(str(library) + "\n", encoding="utf-8")
            _recover(saves, library, mapping, machine.id)
        found = {d.name: records(d, mapping.cache) for d in _key_dirs(library)}
        mine = max((r.vv.get(machine.id, 0) for rs in found.values() for r in rs), default=0)

        def count(vv: VV) -> int:
            return machine.next(max(mine, vv.get(machine.id, 0)))

        def delete(known: dict) -> None:
            """The save ``known`` held was deleted here, once no slot holds it;
            until then what it took in goes to the copies left, so the
            deletion of the last one covers all of them."""
            others = [e for e in mapping.slots.values() if e is not known and e["key"] == known["key"]]
            m = _NAME_RE.match(str(known.get("version", "")))
            latest = max(int(m["time"]) if m else 0, int(known.get("latest", 0)))
            covers = joined(known["vv"], _vv(known.get("covers")))
            if others:
                # Kept apart from what those copies hold: it says only what deleting them deletes.
                for e in others:
                    e["covers"] = joined(_vv(e.get("covers")), covers)
                    e["latest"] = max(int(e.get("latest", 0)), latest)
                return
            n = count(covers)
            when = latest + 1 if latest else ticks()  # just after: any save made since wins over it
            marker = library / known["key"] / f"{_record_name(when, machine.id, n)}.deleted"
            marker.parent.mkdir(parents=True, exist_ok=True)
            _write_once(marker, {"vv": {**covers, machine.id: n}})
            report.deleted.append(known["key"])

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
                    # The slot now holds another save, so the one it held was deleted
                    # (recorded before the slot's new entry, so it can't be lost).
                    del mapping.slots[slot]
                    delete(known)
                twin = next((r for r in reversed(found.get(info.key, []))
                             if not r.deleted and r.digest == content(saves / slot)), None)
                if twin is not None:  # nothing new: it is that version
                    vv = joined(known["vv"], twin.vv) if same else twin.vv
                    mapping.slots[slot] = {**_carried(known if same else None), "key": info.key,
                                           "version": twin.name, "fingerprint": fp, "vv": vv, "held": twin.vv}
                    mapping.save()
                    continue
                n = count(known["vv"] if same else {})
                vv = {**(known["vv"] if same else {}), machine.id: n}
                name = _store(saves / slot, info, machine.id, n, library, vv)
                mapping.verified.add(f"{info.key}/{name}")
                mapping.slots[slot] = {**_carried(known if same else None), "key": info.key, "version": name,
                                       "fingerprint": fp, "vv": vv, "held": vv}
                mapping.save()
                found.setdefault(info.key, []).append(Record(library / info.key / name, info.real_time,
                                                             machine.id, False, vv, content(saves / slot)))
                report.stored.append(f"{info.character}: {info.name}")
        # Only a slot that is gone was deleted; one DFU can't read either is left alone.
        for slot in [s for s in mapping.slots if s not in present]:
            known = mapping.slots.pop(slot)
            delete(known)
            mapping.save()
        unreadable = {mapping.slots[s]["key"] for s in present - set(local) if s in mapping.slots}

        # 2. Each save's winner is the one here.
        for key_dir in _key_dirs(library):
            found[key_dir.name] = recs = records(key_dir, mapping.cache)
            tops = heads(recs)
            best = tops[-1] if tops else None
            if best is None or key_dir.name in unreadable:
                continue
            slot = next((s for s, e in mapping.slots.items() if e["key"] == key_dir.name), None)
            held = mapping.slots.get(slot) if slot is not None else None
            if held is not None and not held["held"]:
                # What it holds is known by name; its vector comes from that record.
                own = next((r for r in recs if not r.deleted and r.name == held.get("version")), None)
                if own is None:
                    continue  # not here yet: decide nothing for this save until it is
                held["held"] = own.vv
                mapping.save()
            # Taking the winner, or holding it, settles every record it beat, too.
            settled = joined(held["vv"] if held else {}, *(t.vv for t in tops))
            # Whether it has the winner goes by what it holds, never by what it
            # settled against: one of those may since have been played on and won.
            if held is not None and (best.name == held.get("version") or
                                     (best.vv != held["held"] and included(best.vv, held["held"]))):
                if not best.deleted and settled != held["vv"]:
                    held["vv"] = settled  # so what is made from it next beats those too
                    mapping.save()
                continue  # it holds the winner, or takes it in already: never go back
            if best.deleted and held is None:
                continue
            if not best.deleted and not _available(best, mapping.verified):
                continue  # the winner's files haven't all arrived: leave this save as it is for now
            with guard() as reason:
                if reason:
                    report.skipped = reason
                    return report
                if not best.deleted and slot is not None and content(saves / slot) == best.digest:
                    # It has those very files already: only what it holds changes, nothing to swap.
                    mapping.slots[slot] = {**_carried(held), "key": key_dir.name, "version": best.name,
                                           "vv": settled, "held": best.vv, "fingerprint": fingerprint(saves / slot)}
                    mapping.save()
                    continue
                if best.deleted:
                    _remove(saves / slot)
                    del mapping.slots[slot]
                    mapping.save()
                    report.removed.append(slot)
                    continue
                target = saves / slot if slot is not None else _free_slot(saves, mapping)
                entry = {**(held or {"key": key_dir.name}), "vv_new": settled}
                if not _place(best, target, mapping, entry):
                    mapping.verified.discard(f"{key_dir.name}/{best.name}")  # check it again next time
                    continue
                info = read_info(target)
                report.placed.append(f"{info.character}: {info.name}" if info else target.name)

        # 3. Remove old versions' files (never a record), but the newest few.
        for recs in found.values():
            _prune(recs)
        mapping.save()
    if report.changed:
        log.info("DFU saves: %s", report.summary())
    return report


def _prune(found: list[Record]) -> None:
    """Remove the files of versions something was made after, but the newest
    few of them. Every ``.id`` and deletion stays, so what wins never changes."""
    tops = {id(r) for r in heads(found)}
    replaced = [r for r in found if not r.deleted and id(r) not in tops and r.path.is_dir()]
    for old in replaced[:-KEEP_VERSIONS]:
        shutil.rmtree(old.path, ignore_errors=True)
