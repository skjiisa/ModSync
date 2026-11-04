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
    ``modsync-save.json`` manifest: character, name, the file list with sizes
    and checksums, and the versions it was made from (``ancestors``, newest
    first). ``<key>`` stands for the character and save name; ``<version>`` is
    the save's own time (``SaveInfo.txt``'s ``realTime``) and the machine that
    made it, so no two machines ever write one path.

``<library>/<key>/<time>-<machine>.deleted``
    The save was deleted on that machine; the file lists the versions the
    deletion did away with.

Which version of a save wins goes by descent first: a version made from
another always replaces it, whatever the clocks of the machines say (DFU's
times are local time, so time zones and the end of summer time would
otherwise let older progress win). Only versions made separately (neither
from the other; say, each machine played on from the same save before they
met) are decided by DFU's clock, and both are kept. A slot is never moved back
to a version its own descends from.

Whenever DFU isn't running, :func:`sync` copies new and changed local saves
into the library, then makes each save's winning version the one in its local
slot: an older local copy is replaced (swapped in one step, so an interrupted
sync never leaves a slot missing), a save new here gets a free number, a
deleted one goes. The version replaced stays in the library (the winner and the
newest few others of each save are kept). A version is used
only once its files match its manifest, checked again just before it goes
into a slot.

Which local slot holds which version, as last synced with one library, is in
``Saves/.modsync-saves-<library>.json``: the Flatpak and a native ModSync on
one machine keep separate libraries and must not take each other's word.
DFU only reads ``SAVE<n>`` folders, so that and ModSync's temporary folders
(``.modsync-*``) are invisible to it.
"""

from __future__ import annotations

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
KEEP_VERSIONS = 3  # of each save besides the winning one, newest first
MAX_ANCESTORS = 64  # how far back a version remembers what it was made from
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


def _manifest(version: Path) -> dict | None:
    try:
        data = json.loads((version / MANIFEST).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("files"), dict) else None
    except (OSError, ValueError):
        return None


def matches(folder: Path, files: dict) -> bool:
    """``folder`` holds exactly what ``files`` (a manifest's) lists, whole."""
    try:
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


def _ancestors(value: object) -> list[str]:
    return [a for a in value if isinstance(a, str) and _RECORD_RE.match(a)] if isinstance(value, list) else []


@dataclass
class Record:
    path: Path
    time: int
    machine: str
    deleted: bool
    ancestors: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def order(self) -> tuple[int, str]:
        return self.time, self.machine


def records(key_dir: Path, verified: dict[str, list[str]] | None = None) -> list[Record]:
    """A save's versions and deletions in the library, oldest first. Versions
    still arriving are left out. ``verified`` maps ``<key>/<version>`` of
    versions already found complete to their ancestors: they're written once,
    so reading them again on every sync would only cost time."""
    out = []
    try:
        entries = list(key_dir.iterdir())
    except OSError:
        return []
    for entry in entries:
        m = _RECORD_RE.match(entry.name)
        if not m:
            continue
        if m["deleted"]:
            if not entry.is_file():
                continue
            try:
                ancestors = _ancestors(json.loads(entry.read_text(encoding="utf-8") or "{}").get("ancestors"))
            except (OSError, ValueError, AttributeError):
                ancestors = []
            out.append(Record(entry, int(m["time"]), m["machine"], True, ancestors))
            continue
        name = f"{key_dir.name}/{entry.name}"
        if verified is not None and name in verified:
            out.append(Record(entry, int(m["time"]), m["machine"], False, verified[name]))
            continue
        if not entry.is_dir() or not complete(entry):
            continue
        ancestors = _ancestors(_manifest(entry).get("ancestors"))
        if verified is not None:
            verified[name] = ancestors
        out.append(Record(entry, int(m["time"]), m["machine"], False, ancestors))
    return sorted(out, key=lambda r: r.order)


def heads(found: list[Record]) -> list[Record]:
    """The records no other was made from, oldest first."""
    replaced = {a for r in found for a in r.ancestors}
    return [r for r in found if r.name not in replaced]


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


def mapping_path(saves: Path, library: Path) -> Path:
    library_id = hashlib.sha256(str(library.resolve()).encode()).hexdigest()[:12]
    return saves / f"{MAPPING_PREFIX}{library_id}.json"


class _Mapping:
    def __init__(self, path: Path, library: Path, machine: str) -> None:
        self.path = path
        self.library = library
        self.machine = machine
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        slots = data.get("slots")
        self.slots: dict[str, dict] = slots if isinstance(slots, dict) else {}
        verified = data.get("verified")
        self.verified: dict[str, list[str]] = {k: _ancestors(v) for k, v in verified.items()} \
            if isinstance(verified, dict) else {}

    def save(self) -> None:
        """After every change to a slot, so an interrupted sync loses none of them."""
        self.verified = {v: a for v, a in self.verified.items() if (self.library / v).is_dir()}  # pruned ones go
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"library": str(self.library), "slots": self.slots, "verified": self.verified},
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
    """Finish what an interrupted sync left half done: a slot caught mid-swap
    (where the swap couldn't be one step) gets its old contents back, so it
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


_RENAME_EXCHANGE = 2
_libc = None


def _exchange(a: Path, b: Path) -> bool:
    """Swap two folders in one step (Linux's renameat2 RENAME_EXCHANGE).
    False where that isn't available."""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        at_fdcwd = -100
        result = _libc.renameat2(at_fdcwd, os.fsencode(a), at_fdcwd, os.fsencode(b), _RENAME_EXCHANGE)
    except (OSError, AttributeError):
        return False
    if result != 0:
        err = ctypes.get_errno()
        if err in (22, 38, 95):  # EINVAL, ENOSYS, EOPNOTSUPP: this filesystem or kernel can't
            return False
        raise OSError(err, os.strerror(err), str(a))
    return True


def _store(slot: Path, info: Info, machine: str, library: Path, ancestors: list[str]) -> Path:
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
                                                 "ancestors": ancestors[:MAX_ANCESTORS],
                                                 "files": files}, indent=2) + "\n", encoding="utf-8")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(incoming, dest)
    return dest


def _place(version: Path, slot: Path) -> bool:
    """Make ``slot`` hold exactly ``version``. The copy is checked against the
    manifest first (the version may have changed since it was found whole:
    another machine pruning it, say), and swapped in in one step. False,
    changing nothing, when the copy isn't whole."""
    manifest = _manifest(version)
    if manifest is None:
        return False
    incoming = slot.with_name(f".modsync-incoming-{slot.name}")
    shutil.rmtree(incoming, ignore_errors=True)
    shutil.copytree(version, incoming, ignore=shutil.ignore_patterns(MANIFEST, ".syncthing.*", "*.sync-conflict-*"))
    if not matches(incoming, manifest["files"]) or len(_files(incoming)) != len(manifest["files"]):
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


def _free_slot(saves: Path) -> Path:
    n = 0
    while (saves / f"SAVE{n}").exists():
        n += 1
    return saves / f"SAVE{n}"


def _lineage(entry: dict) -> list[str]:
    """The version a slot holds and what that was made from, newest first."""
    return [entry["version"], *_ancestors(entry.get("ancestors"))] if entry.get("version") else []


def sync(saves: Path, library: Path, machine: str, *, busy: Callable[[], str] | None = None) -> Report:
    """Bring ``saves`` (DFU's folder) and ``library`` (the synced one) in line.
    ``machine`` names this machine in the versions it writes; it must differ
    from every other machine's. ``busy`` says (as a reason) why DFU may be
    writing saves right now, checked once the lock is held; nothing is done
    then."""
    report = Report()
    if not saves.is_dir():
        report.skipped = f"{saves} doesn't exist yet."
        return report
    library.mkdir(parents=True, exist_ok=True)
    with _locked(saves):
        reason = busy() if busy is not None else ""
        if reason:
            report.skipped = reason
            return report
        _recover(saves, library)
        mapping = _Mapping(mapping_path(saves, library), library, machine)
        local, present = _slots(saves)

        # 1. What changed here goes into the library.
        for slot, (info, fp) in sorted(local.items()):
            known = mapping.slots.get(slot)
            if known and known.get("fingerprint") == fp:
                continue
            same = known is not None and known.get("key") == info.key
            if known and not same:
                # The slot now holds another save: the one it held was deleted.
                _delete(library, known, machine, report)
            lineage = _lineage(known) if same else []
            version = _store(saves / slot, info, machine, library, lineage)
            mapping.verified[f"{info.key}/{version.name}"] = lineage[:MAX_ANCESTORS]
            mapping.slots[slot] = {"key": info.key, "version": version.name, "fingerprint": fp,
                                   "ancestors": lineage[:MAX_ANCESTORS]}
            mapping.save()
            report.stored.append(f"{info.character}: {info.name}")
        # Only a slot that is gone was deleted; one DFU can't read either is left alone.
        for slot in [s for s in mapping.slots if s not in present]:
            _delete(library, mapping.slots.pop(slot), machine, report)
            mapping.save()
        unreadable = {mapping.slots[s]["key"] for s in present - set(local) if s in mapping.slots}

        # 2. Each save's winning version is the one here.
        by_key = {m["key"]: slot for slot, m in mapping.slots.items()}
        seen: dict[Path, list[Record]] = {}
        for key_dir in sorted(p for p in library.iterdir() if p.is_dir() and not p.name.startswith(".")):
            found = seen[key_dir] = records(key_dir, mapping.verified)
            best = winner(found)
            if best is None or key_dir.name in unreadable:
                continue
            slot = by_key.get(key_dir.name)
            held = _lineage(mapping.slots[slot]) if slot is not None else []
            if best.name in held:
                continue  # it holds that version, or one made from it: never go back
            if best.deleted:
                if slot is not None:
                    shutil.rmtree(saves / slot)
                    del mapping.slots[slot]
                    mapping.save()
                    report.removed.append(slot)
                continue
            target = saves / slot if slot is not None else _free_slot(saves)
            if not _place(best.path, target):
                mapping.verified.pop(f"{key_dir.name}/{best.name}", None)  # check it again next time
                continue
            info = read_info(target)
            mapping.slots[target.name] = {"key": key_dir.name, "version": best.name,
                                          "fingerprint": fingerprint(target), "ancestors": best.ancestors}
            mapping.save()
            report.placed.append(f"{info.character}: {info.name}" if info else target.name)

        # 3. Keep what nothing replaced, and the newest few others of each save.
        for found in seen.values():
            _prune(found)
        mapping.save()
    if report.changed:
        log.info("DFU saves: %s", report.summary())
    return report


def _delete(library: Path, known: dict, machine: str, report: Report) -> None:
    """Record that the save ``known`` held was deleted here, and what it held:
    the deletion replaces that version and those it came from. A version made
    separately elsewhere is weighed against it by time, so the marker is dated
    just after the version deleted (when it was deleted isn't known; ModSync
    may only notice much later) and any save made since wins."""
    lineage = _lineage(known)
    m = _RECORD_RE.match(lineage[0]) if lineage else None
    when = int(m["time"]) + 1 if m else ticks()
    marker = library / known["key"] / f"{when:020d}-{machine}.deleted"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"ancestors": lineage[:MAX_ANCESTORS]}) + "\n", encoding="utf-8")
    report.deleted.append(known["key"])


def _prune(found: list[Record]) -> None:
    """Keep the winner and the newest few other versions, to go back to by
    hand. A deletion that doesn't decide the save says nothing any more."""
    best = winner(found)
    versions = [r for r in found if not r.deleted and r is not best]
    for old in versions[:-KEEP_VERSIONS]:
        shutil.rmtree(old.path, ignore_errors=True)
    for marker in (r for r in found if r.deleted and r is not best):
        marker.path.unlink(missing_ok=True)
