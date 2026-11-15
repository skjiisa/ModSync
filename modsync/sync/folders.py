"""The games' folders: what ModSync syncs besides Skyrim's MO2 vault.

Pairing is machine-wide. Machines that have paired (with a code, the LAN PIN,
or by joining the vault) are Syncthing devices, and every game folder this
machine syncs is shared with all of them. A game describes its folders with
a :class:`FolderSpec`. Each one has a fixed id, ``modsync-<key>``, so a machine
offered one knows what it is and where it goes here without a code having to
name it. An offer from a paired machine is taken up as soon as the game is
set up here (its spec finds a local path); until then it waits, and Syncthing
keeps offering it. A folder stopped here is remembered as declined, so offers
of it are left alone until it is started again.

A machine can also keep a copy of a folder whose game isn't set up there
(``hold``, or every one offered with ``State.hold_all``). The copy lives under
ModSync's data folder, is receive-only, and is never used here: the game's
own hooks (``on_share``, ``tidy``, ``in_use``, ``accepts``, ``on_stop``) leave
it alone. It's there for the other machines, which sync through it, so a
machine that is often on passes changes between two that rarely are at the
same time. Syncthing passes on what a receive-only folder has received.

Skyrim's vault is set up as it was (``ModSyncService``): a random folder id
that the pairing code carries, a copy that starts receive-only, and MO2-aware
conflicts. Only a copy of it kept for other machines comes through here. Its
key is the id's random part (:func:`vault_spec`), so it's kept like any other
folder, apart from the vault this machine uses (``State.folder_id``), and
joining the vault later uses the copy (:func:`kept_copy_gives_way`).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from modsync.state import State
from modsync.sync import conflicts

if TYPE_CHECKING:  # `modsync doctor` reads FOLDER_PREFIX without httpx
    from modsync.sync.api import SyncthingClient

log = logging.getLogger(__name__)

FOLDER_PREFIX = "modsync-"
HELD_DIR = "held"  # under ModSync's data folder: the copies kept for other machines


@dataclass(frozen=True)
class FolderSpec:
    key: str  # e.g. "dfu-mods"; the folder id is "modsync-<key>"
    label: str  # how Syncthing and ModSync name it
    path: Callable[[], Path | None]  # where it is on this machine; None while the game isn't set up
    ignore: tuple[str, ...]  # its .stignore, one pattern per line
    # The synced paths, relative to the folder: where conflict copies can turn
    # up, and what a move to another place carries over.
    content: tuple[str, ...] = ()
    on_share: Callable[[Path], None] | None = None  # run on the machine that starts a new shared folder
    in_use: Callable[[], bool] | None = None  # the game is running: settling a conflict waits
    tidy: Callable[[Path], object] | None = None  # run on the folder whenever its status is read
    on_stop: Callable[[], None] | None = None  # run once it has stopped syncing here
    # Whether a paired machine's offer may fill the folder here by itself; when
    # it says no, the offer waits for 'share' (None: always).
    accepts: Callable[[Path], bool] | None = None

    @property
    def folder_id(self) -> str:
        return FOLDER_PREFIX + self.key

    def stignore_text(self) -> str:
        return "\n".join(self.ignore) + "\n"


_specs: list[FolderSpec] | None = None


def specs() -> list[FolderSpec]:
    """Every game folder ModSync knows: Daggerfall Unity's two, and the mods
    of each game ModSync runs Mod Organizer 2 for (modsync.mo2games)."""
    global _specs
    if _specs is None:
        from modsync import dfu, mo2games  # import this module's users; loaded on first use

        _specs = [dfu.MODS_FOLDER, dfu.SAVES_FOLDER, *(game.folder for game in mo2games.all_games().values())]
    return _specs


def spec_for(key: str) -> FolderSpec | None:
    return next((s for s in specs() if s.key == key), None) or vault_spec(key)


def _spec_for_id(folder_id: str) -> FolderSpec | None:
    if not folder_id.startswith(FOLDER_PREFIX):
        return None
    return spec_for(folder_id.removeprefix(FOLDER_PREFIX))


# The random part of a Skyrim vault's folder id ("modsync-" + 16 hex digits,
# ModSyncService._new_folder_id). No game's key looks like this.
_VAULT_KEY = re.compile(r"[0-9a-f]{16}")
VAULT_LABEL = "Skyrim SE mods (ModSync)"


def is_vault(key: str) -> bool:
    """Whether ``key`` is a Skyrim vault's, not a game folder's."""
    return bool(_VAULT_KEY.fullmatch(key))


def _skyrim_instance() -> Path | None:
    """Skyrim's MO2 instance here, the game being set up when there is one."""
    path = State.load().instance_path
    return Path(path) if path else None


def _no_mods(root: Path) -> bool:
    """An MO2 instance with no mods or downloads of its own (what joining
    would ask to copy over or merge with): a kept copy can move into it."""
    for name in ("mods", "downloads"):
        try:
            if any(not p.name.startswith(".") and p.suffix != ".meta" for p in (root / name).iterdir()):
                return False
        except OSError:
            pass
    return True


def vault_spec(key: str) -> FolderSpec | None:
    """A Skyrim vault, as far as keeping a copy of it goes. The vault itself
    syncs through ``ModSyncService`` once Skyrim is set up here (its offers
    wait for a join, see :func:`take_offers`), so this only knows where the
    game is, what syncs, and when the instance is empty enough for a kept
    copy to move into."""
    if not is_vault(key):
        return None
    from modsync.sync import stignore  # stdlib only

    return FolderSpec(
        key=key,
        label=VAULT_LABEL,
        path=_skyrim_instance,
        ignore=tuple(stignore.PATTERNS),
        content=(*stignore.CONTENT_DIRS, "categories.dat", "modsync-vault.json"),
        accepts=_no_mods,
    )


def offered_vaults(client: SyncthingClient) -> list[str]:
    """The keys of the Skyrim vaults paired machines offer here."""
    try:
        pending = client.pending_folders() or {}
    except Exception as exc:
        log.info("pending folders unavailable: %s", exc)
        return []
    st = State.load()
    paired = set(peers(client)) - set(st.forgotten_devices)
    return sorted(fid.removeprefix(FOLDER_PREFIX) for fid, info in pending.items()
                  if fid.startswith(FOLDER_PREFIX) and is_vault(fid.removeprefix(FOLDER_PREFIX))
                  and fid != st.folder_id and set((info or {}).get("offeredBy") or {}) & paired)


def kept_vault(st: State | None = None) -> str | None:
    """The key of the Skyrim vault a copy is kept of here, if one is."""
    st = st or State.load()
    return next((k for k in st.held_folders if is_vault(k)), None)


@dataclass
class FolderStatus:
    spec: FolderSpec
    path: str
    state: str | None = None  # Syncthing's folder state; None when it didn't answer
    completion: float | None = None
    need_items: int = 0
    stopping: bool = False  # stopped here; Syncthing lets go of it once it answers
    conflicts: int = 0  # conflict copies waiting for a choice
    held: bool = False  # a copy kept for other machines; the game isn't set up here


def held_root(spec: FolderSpec) -> Path:
    """Where a copy of ``spec`` kept for other machines goes on this machine."""
    from modsync import config

    return config.data_dir() / HELD_DIR / spec.folder_id


def _synced(st: State) -> dict[str, str]:
    """Every game folder in Syncthing here, used or kept: key to path."""
    return {**st.held_folders, **st.game_folders}


def device_entries(ids) -> list[dict]:
    """Syncthing's list of a folder's devices, in order, each once."""
    return [{"deviceID": d, "introducedBy": "", "encryptionPassword": ""} for d in dict.fromkeys(ids)]


def peers(client: SyncthingClient) -> list[str]:
    """Every paired machine: each Syncthing device but this one."""
    me = client.my_id()
    return [d["deviceID"] for d in client.devices() if d.get("deviceID") and d["deviceID"] != me]


def seen_peers(client: SyncthingClient) -> list[str]:
    """The paired machines that have connected here at least once, leaving out
    forgotten ones. Another machine passes on every machine it lists, which
    can include an identity that will never connect again (a reinstalled
    machine's old one); the games' folders aren't shared with those."""
    from modsync.sync.health import seen_at  # stdlib only; health imports nothing from here

    forgotten = set(State.load().forgotten_devices)
    everyone = [d for d in peers(client) if d not in forgotten]
    try:
        stats = client.device_stats()
        conns = client.connections().get("connections", {})
    except Exception as exc:
        log.info("can't tell which machines have connected (%s); counting them all", exc)
        return everyone
    return [d for d in everyone if (conns.get(d) or {}).get("connected") or seen_at(stats.get(d) or {}) is not None]


def write_stignore(spec: FolderSpec, root: Path) -> None:
    target = root / ".stignore"
    text = spec.stignore_text()
    try:
        if target.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    target.write_text(text, encoding="utf-8")


def _prepare(spec: FolderSpec, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_stignore(spec, root)


def _relocate(client: SyncthingClient, spec: FolderSpec, folder: dict, root: Path) -> None:
    """Move a configured folder to ``root``, keeping everything else about it
    (machines, paused, versioning, type).

    Pointing it at the new place would read every file not there yet as
    deleted, and send those deletions to the others. Removed and added again,
    it starts with no record here: files the others have arrive, and nothing
    is deleted anywhere. ``root`` is set up first, and if the folder can't be
    added there the old one is put back, so a failure loses nothing."""
    _prepare(spec, root)
    old = Path(folder["path"]) if folder.get("path") else None
    if old is not None and old != root and old.is_dir():
        _carry_over(spec, old, root)
    moved = {**folder, "path": str(root)}
    client.delete_folder(spec.folder_id)
    try:
        client.put_folder(moved)
    except Exception:
        client.put_folder(folder)
        raise
    log.info("%s moved from %s to %s", spec.folder_id, folder.get("path"), root)


def _put(client: SyncthingClient, spec: FolderSpec, root: Path, device_ids: list[str], *,
         receive_only: bool = False) -> None:
    """Create the folder, or add machines to it as it is: its other settings
    (paused, versioning, ...) and the machines it already has stay. One that
    is configured somewhere else here is moved, never repointed. A new one is
    ``receive_only`` from its first PUT, so nothing found here is announced."""
    try:
        folder = client.get_folder(spec.folder_id)
    except Exception:
        folder = None
    if folder is None:
        _prepare(spec, root)
        folder = client.default_folder()
        folder["id"] = spec.folder_id
        folder["label"] = spec.label
        folder["path"] = str(root)
        if receive_only:
            folder["type"] = "receiveonly"
        folder["devices"] = device_entries([client.my_id(), *device_ids])
        client.put_folder(folder)
        return
    have = [d["deviceID"] for d in folder.get("devices", [])]
    folder["devices"] = device_entries([client.my_id(), *have, *device_ids])
    if folder.get("path") and Path(folder["path"]) != root:
        _relocate(client, spec, folder, root)
        return
    _prepare(spec, root)
    folder["path"] = str(root)
    client.put_folder(folder)


def _remember(key: str, path: Path, *, held: bool = False) -> None:
    def change(st: State) -> None:
        used = {k: v for k, v in st.game_folders.items() if k != key}
        kept = {k: v for k, v in st.held_folders.items() if k != key}
        (kept if held else used)[key] = str(path)
        st.game_folders, st.held_folders = used, kept
        st.declined_folders = [k for k in st.declined_folders if k != key]

    State.change(change)


def _forget(key: str) -> None:
    def change(st: State) -> None:
        st.game_folders = {k: v for k, v in st.game_folders.items() if k != key}
        st.held_folders = {k: v for k, v in st.held_folders.items() if k != key}

    State.change(change)


def _decline(key: str) -> None:
    def change(st: State) -> None:
        st.declined_folders = sorted({*st.declined_folders, key})

    State.change(change)


def stopping(key: str, st: State | None = None) -> bool:
    """Stopped here, but not yet removed from Syncthing (it wasn't answering)."""
    st = st or State.load()
    return key in _synced(st) and key in st.declined_folders


def share(client: SyncthingClient, spec: FolderSpec) -> Path:
    """Start syncing ``spec`` from this machine, with every paired machine.
    A copy kept here for other machines gives way: the folder syncs where the
    game is from now on (:func:`kept_copy_gives_way`)."""
    if is_vault(spec.key):
        raise RuntimeError("Skyrim's mods sync once you join them: 'modsync sync join kept <instance-dir>', "
                           "or 'Use the mods kept here' on the Sync page.")
    root = spec.path()
    if root is None:
        raise RuntimeError(f"{spec.label}: set the game up on this machine first.")
    with kept_copy_gives_way(client, spec, root) as kept:
        joined = (kept is not None and kept.had_content) or _offered(client, spec)
        if spec.on_share is not None and not joined:
            spec.on_share(root)  # a new shared folder; one already offered is joined as it is
        _put(client, spec, root, peers(client))
        _remember(spec.key, root)
    log.info("sharing %s (%s) from %s", spec.folder_id, spec.label, root)
    return root


@dataclass
class KeptCopy:
    """A copy kept here that gave way to the game's folder (see
    :func:`kept_copy_gives_way`)."""

    path: Path
    folder: dict | None  # its Syncthing folder before it gave way, to put back
    had_content: bool  # it had files: the folder exists on other machines
    moved: list[_Move] = field(default_factory=list)


@contextmanager
def kept_copy_gives_way(client: SyncthingClient, spec: FolderSpec, root: Path):
    """Around setting ``spec`` up to sync at ``root``, where the game is: a
    copy kept here for other machines leaves Syncthing first. When ``root``
    has nothing of its own yet (a new MO2 instance) and the kept copy is
    current, its files move there, so nothing downloads again
    (:func:`_use_kept`); otherwise they stay where they were. Yields the
    :class:`KeptCopy`, or None when no copy is kept here.

    Until the block ends, offers of the folder are left alone, so another
    process (the background service) can't keep it again while it moves. If
    the block fails, the moves are undone and the copy is kept as it was, so
    the other machines still sync through it."""
    path = State.load().held_folders.get(spec.key)
    if path is None:
        yield None
        return
    try:
        before = client.get_folder(spec.folder_id)
    except Exception:
        before = None  # not in Syncthing (removed by hand): nothing to put back
    kept = KeptCopy(Path(path), before, _has_content(Path(path)))
    current = _kept_is_current(client, spec, before)
    _decline(spec.key)
    try:
        _release(client, spec)
    except Exception:
        _undecline(spec.key)
        raise
    try:
        if current:
            kept.moved = _use_kept(spec, kept.path, root)
        yield kept
    except BaseException:
        _undo_moves(kept.moved)
        _restore_kept(client, spec, before, kept.path)
        raise
    _undecline(spec.key)
    if kept.moved:
        _tidy_kept(kept.path)
        log.info("%s now uses the copy that was kept here: %s moved to %s", spec.folder_id, kept.path, root)


def _undecline(key: str) -> None:
    def change(st: State) -> None:
        st.declined_folders = [k for k in st.declined_folders if k != key]

    State.change(change)


def _kept_is_current(client: SyncthingClient, spec: FolderSpec, folder: dict | None) -> bool:
    """Whether the kept copy has everything the other machines have, as far
    as Syncthing can tell: nothing left to receive, and a machine it's shared
    with connected now. A copy that missed a deletion elsewhere would bring
    the deleted files back once it syncs from the game's folder."""
    if folder is None:
        return False
    try:
        status = client.folder_status(spec.folder_id)
        connections = client.connections().get("connections", {})
        me = client.my_id()
    except Exception as exc:
        log.info("can't tell whether the copy of %s kept here is current: %s", spec.folder_id, exc)
        return False
    if status.get("state") != "idle" or int(status.get("needTotalItems") or 0):
        return False
    return any((connections.get(d.get("deviceID")) or {}).get("connected")
               for d in folder.get("devices", []) if d.get("deviceID") and d.get("deviceID") != me)


@dataclass(frozen=True)
class _Move:
    source: Path  # in the kept copy
    target: Path  # in the game's folder
    replaced: Path | None  # where what was at ``target`` went, if anything was


# Where what a kept copy replaces in the game's folder goes: inside the kept
# copy's folder, which Syncthing no longer has, so it never syncs anywhere.
REPLACED_DIR = ".modsync-replaced"


def _empty_here(spec: FolderSpec, root: Path) -> bool:
    """Whether the game's folder has nothing of its own the kept copy would
    have to merge with: the game's own rule (an MO2 instance with no mods
    installed), else nothing at all at the synced paths."""
    if spec.accepts is not None:
        return spec.accepts(root)
    for rel in spec.content:
        path = root / rel
        if path.is_file():
            return False
        if path.is_dir() and any(files for _top, _dirs, files in os.walk(path)):
            return False
    return True


def _synced_paths(spec: FolderSpec, root: Path) -> list[str]:
    """``spec.content`` as it stands in ``root``: each synced file with the
    conflict copies Syncthing left beside it, which sync too and still wait
    for a choice. (A synced folder holds its own.)"""
    paths = []
    for rel in spec.content:
        paths.append(rel)
        path = root / rel
        if path.is_dir() or "." not in path.name:
            continue
        stem, dot, ext = path.name.rpartition(".")
        try:
            paths += sorted(str(Path(rel).with_name(p.name)) for p in path.parent.iterdir()
                            if p.name.startswith(f"{stem}.sync-conflict-") and p.name.endswith(f"{dot}{ext}"))
        except OSError:
            pass
    return paths


def _same_disk(a: Path, b: Path) -> bool:
    """Whether a rename between the two can't fail half way: same filesystem."""
    while not b.exists() and b != b.parent:
        b = b.parent
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _use_kept(spec: FolderSpec, kept: Path, root: Path) -> list[_Move]:
    """Move the kept copy's synced files into the game's folder, which has
    nothing of its own yet. Only by renaming, on the same disk, so each move
    happens whole or not at all and the files stay exactly as Syncthing knows
    them; on another disk nothing moves, and the files download again. What
    the game's folder has at those paths (a new MO2's empty profile, say)
    goes to :data:`REPLACED_DIR` in the kept copy's folder. Returns the
    moves, for :func:`_undo_moves`."""
    if not spec.content or not kept.is_dir() or not _empty_here(spec, root):
        return []
    if not _same_disk(kept, root):
        log.info("the copy of %s kept at %s is on another disk than %s; its files download again",
                 spec.folder_id, kept, root)
        return []
    aside = kept / REPLACED_DIR / time.strftime("%Y%m%d-%H%M%S")
    moved: list[_Move] = []
    try:
        for rel in _synced_paths(spec, kept):
            source, target = kept / rel, root / rel
            if not (source.exists() or source.is_symlink()):
                continue
            replaced = None
            if target.exists() or target.is_symlink():
                replaced = aside / rel
                replaced.parent.mkdir(parents=True, exist_ok=True)
                os.rename(target, replaced)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, target)
            except Exception:
                if replaced is not None:
                    os.rename(replaced, target)
                raise
            moved.append(_Move(source, target, replaced))
    except Exception:
        _undo_moves(moved)
        raise
    return moved


def _undo_moves(moved: list[_Move]) -> None:
    """Put what :func:`_use_kept` moved back where it was."""
    for move in reversed(moved):
        try:
            os.rename(move.target, move.source)
            if move.replaced is not None:
                os.rename(move.replaced, move.target)
        except OSError as exc:
            log.warning("couldn't move %s back to %s: %s", move.target, move.source, exc)


def _tidy_kept(kept: Path) -> None:
    """Remove the kept copy's folder once only Syncthing's own files are left
    in it. What a move replaced stays, and so does the folder with it, as do
    old versions Syncthing archived there (.stversions)."""
    if _has_content(kept):
        return
    versions = kept / ".stversions"
    if versions.is_dir() and any(files for _top, _dirs, files in os.walk(versions)):
        return
    try:
        shutil.rmtree(kept)
    except OSError as exc:
        log.info("couldn't remove the emptied copy at %s: %s", kept, exc)


def hold(client: SyncthingClient, spec: FolderSpec) -> Path:
    """Keep a copy of ``spec`` here for the other machines, the game not
    being set up here. Shared with every paired machine; receive-only."""
    st = State.load()
    if spec.key in st.game_folders and not stopping(spec.key, st):
        raise RuntimeError(f"{spec.label} already syncs here, where the game is.")
    if spec.path() is not None:
        if is_vault(spec.key):
            raise RuntimeError("Skyrim's Mod Organizer 2 is set up here, so its mods sync where it is "
                               "('modsync sync join').")
        raise RuntimeError(f"{spec.label}: the game is set up here, so its folder syncs where the game is "
                           f"('modsync sync share {spec.key}').")
    if spec.folder_id == st.folder_id:
        raise RuntimeError(f"{spec.label} already syncs here.")
    root = held_root(spec)
    _put(client, spec, root, peers(client), receive_only=True)
    _remember(spec.key, root, held=True)
    log.info("keeping a copy of %s (%s) at %s", spec.folder_id, spec.label, root)
    return root


def hold_all(client: SyncthingClient | None, on: bool) -> list[str]:
    """Keep a copy of every game's folder paired machines offer (games not
    set up here), now and later; or stop taking new ones up. Copies already
    kept stay either way. Turned on, it takes up again the folders stopped
    here whose game isn't set up here: "every" includes those. Returns the
    keys taken up now."""
    if not on:
        State.update(hold_all=False)
        return []
    elsewhere = {s.key for s in specs() if s.path() is None}
    no_skyrim = _skyrim_instance() is None

    def change(st: State) -> None:
        st.hold_all = True
        st.declined_folders = [k for k in st.declined_folders
                               if (k not in elsewhere and not (no_skyrim and is_vault(k))) or stopping(k, st)]

    State.change(change)
    return take_offers(client)


def _release(client: SyncthingClient, spec: FolderSpec) -> None:
    """Take the copy kept here out of Syncthing and the state. Its files stay."""
    if not _remove(client, spec):
        raise RuntimeError(f"Syncthing didn't let go of the copy of {spec.label} kept here. Try again.")
    _forget(spec.key)
    log.info("the copy of %s kept here gives way", spec.folder_id)


def _restore_kept(client: SyncthingClient, spec: FolderSpec, folder: dict | None, root: Path) -> None:
    """Sharing failed after the kept copy gave way: keep it as it was, so the
    other machines still sync through it."""
    try:
        if folder is not None:
            client.put_folder(folder)
        _remember(spec.key, root, held=True)
        log.info("sharing %s failed; the copy kept here stays", spec.folder_id)
    except Exception as exc:
        log.warning("sharing %s failed, and the copy kept at %s couldn't be put back: %s",
                    spec.folder_id, root, exc)


def _has_content(root: Path) -> bool:
    """Anything in the folder besides what Syncthing and ModSync put there."""
    try:
        return any(p.name not in {".stignore", ".stfolder", ".stversions"} for p in root.iterdir())
    except OSError:
        return False


def _offered(client: SyncthingClient, spec: FolderSpec) -> bool:
    """A paired machine already offers this folder."""
    try:
        offer = (client.pending_folders() or {}).get(spec.folder_id) or {}
    except Exception:
        return False
    return bool(set(offer.get("offeredBy") or {}) & set(peers(client)))


def _carry_over(spec: FolderSpec, old: Path, new: Path) -> None:
    """Copy the synced content of ``old`` to ``new`` where ``new`` has nothing
    yet, so what hadn't reached another machine moves too. Nothing at
    ``new`` is replaced, and ``old`` stays as it is."""
    for rel in _synced_paths(spec, old):
        source = old / rel
        if source.is_file() and not (new / rel).exists():
            (new / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, new / rel)
            continue
        for top, _dirs, files in os.walk(source):
            for name in files:
                src = Path(top) / name
                dest = new / src.relative_to(old)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)


def _remove(client: SyncthingClient, spec: FolderSpec) -> bool:
    """Take the folder out of Syncthing. Files stay. True once it's gone."""
    try:
        client.delete_folder(spec.folder_id)
        return True
    except Exception as exc:
        try:
            if all(f.get("id") != spec.folder_id for f in client.folders()):
                return True  # it was gone already
        except Exception:
            pass
        log.warning("could not remove %s from Syncthing: %s", spec.folder_id, exc)
        return False


def stop(client: SyncthingClient | None, spec: FolderSpec) -> bool:
    """Stop syncing ``spec`` here. Files stay on disk, and offers of it from
    other machines are no longer taken up. Returns whether Syncthing has let
    go of it; if not (``client`` is None, or it refused), the folder stays on
    record as stopping, and :func:`refresh` removes it once Syncthing answers."""
    _decline(spec.key)
    if client is None or not _remove(client, spec):
        log.info("stopping %s once Syncthing answers", spec.folder_id)
        return False
    _finish_stop(spec)
    return True


def _finish_stop(spec: FolderSpec) -> None:
    """Syncthing has let go of it: forget it here, and let the game tidy up
    (only after a folder the game used, never a kept copy). Nothing happens
    if it was taken up again meanwhile (shared or kept: no longer declined),
    say by the app while the background service retried an earlier stop."""
    used = []

    def change(st: State) -> None:
        if spec.key not in st.declined_folders:
            return
        used.append(spec.key in st.game_folders)
        st.game_folders = {k: v for k, v in st.game_folders.items() if k != spec.key}
        st.held_folders = {k: v for k, v in st.held_folders.items() if k != spec.key}

    State.change(change)
    if not used:
        log.info("%s was taken up again before its stop finished", spec.folder_id)
        return
    if spec.on_stop is not None and used[0]:
        try:
            spec.on_stop()
        except Exception as exc:
            log.warning("after stopping %s: %s", spec.folder_id, exc)
    log.info("stopped syncing %s", spec.folder_id)


def share_with(client: SyncthingClient, device_id: str) -> list[str]:
    """Add a newly paired machine to every game folder synced or kept here."""
    return _share_with_all(client, [device_id])


def share_with_peers(client: SyncthingClient) -> list[str]:
    """Add every paired machine that has connected here to every game folder
    synced here. A machine another one introduced arrives without being
    paired here, so nothing else shares the folders with it; it gets them
    once it has connected. Returns the keys changed."""
    return _share_with_all(client, seen_peers(client))


def _share_with_all(client: SyncthingClient, device_ids: list[str]) -> list[str]:
    added = []
    st = State.load()
    for key in _synced(st):
        spec = spec_for(key)
        if spec is None or stopping(key, st):
            continue
        try:
            folder = client.get_folder(spec.folder_id)
        except Exception:
            continue  # not in Syncthing (removed by hand): take_offers or share puts it back
        ids = [d["deviceID"] for d in folder.get("devices", [])]
        if set(device_ids) <= set(ids):
            continue
        folder["devices"] = device_entries([*ids, *device_ids])
        client.put_folder(folder)
        added.append(key)
    return added


def take_offers(client: SyncthingClient) -> list[str]:
    """Sync the game folders paired machines offer, where the game is set up
    here and the folder wasn't stopped here; with ``hold_all``, keep a copy
    of the others. Returns their keys."""
    if not specs():
        return []
    try:
        pending = client.pending_folders() or {}
    except Exception as exc:
        log.info("pending folders unavailable: %s", exc)
        return []
    st = State.load()
    paired = set(peers(client)) - set(st.forgotten_devices)
    share = set(seen_peers(client))
    taken = []
    for folder_id, info in pending.items():
        spec = _spec_for_id(folder_id)
        if spec is None or spec.key in st.declined_folders or spec.key in _synced(st):
            continue
        offered_by = [d for d in ((info or {}).get("offeredBy") or {}) if d in paired]
        if not offered_by:
            continue
        if folder_id == st.folder_id:
            continue  # the vault this machine uses, left by hand: ModSyncService puts it back
        root = spec.path()
        held = root is None
        if held:
            if not st.hold_all:
                continue
            root = held_root(spec)
        elif is_vault(spec.key):
            log.info("%s is offered; Skyrim is set up here, so it waits for a join", folder_id)
            continue
        elif spec.accepts is not None and not spec.accepts(root):
            log.info("%s is offered, but %s has content of its own; waiting for 'share'", folder_id, root)
            continue
        _put(client, spec, root, sorted(share | set(offered_by)), receive_only=held)
        _remember(spec.key, root, held=held)
        taken.append(spec.key)
        log.info("%s %s from %s at %s", "keeping a copy of" if held else "took up", folder_id,
                 ", ".join(d[:7] for d in offered_by), root)
    return taken


def _retry_stop(client: SyncthingClient, spec: FolderSpec) -> None:
    """Finish a stop Syncthing refused before. Checked again and removed
    under the state's lock, so a 'share' or 'hold' in another process can't
    take the folder up again between the check and the removal, and have its
    new folder removed instead."""
    with State.held() as st:
        removed = stopping(spec.key, st) and _remove(client, spec)
    if removed:
        _finish_stop(spec)


def refresh(client: SyncthingClient) -> None:
    """Finish stopping folders Syncthing didn't let go of before, keep each
    synced folder's .stignore current with this version of ModSync, and
    follow a game that moved on this machine (say, reinstalled elsewhere)."""
    st = State.load()
    for key, recorded in st.held_folders.items():
        spec = spec_for(key)
        if spec is None:
            continue
        if stopping(key, st):
            _retry_stop(client, spec)
            continue
        try:
            write_stignore(spec, Path(recorded))
        except OSError:
            pass
    for key, recorded in st.game_folders.items():
        spec = spec_for(key)
        if spec is None:
            continue
        if stopping(key, st):
            _retry_stop(client, spec)
            continue
        root = spec.path()
        if root is None:
            continue
        if str(root) != recorded:
            if spec.in_use is not None and spec.in_use():
                log.info("%s moved to %s; it follows once the game has closed", spec.folder_id, root)
                continue
            try:
                folder = client.get_folder(spec.folder_id)
                _relocate(client, spec, folder, root)
            except Exception as exc:
                log.warning("%s moved to %s, but it can't be set up there yet: %s", spec.folder_id, root, exc)
                continue
            _remember(key, root)
            continue
        try:
            write_stignore(spec, root)
        except OSError:
            pass


def _tidy(spec: FolderSpec, key: str, path: str, st: State) -> None:
    if spec.tidy is not None and not stopping(key, st):
        try:
            spec.tidy(Path(path))
        except Exception as exc:  # neither a status read nor the background service may fail over it
            log.warning("could not tidy %s: %s", spec.folder_id, exc)


def tidy() -> None:
    """Each game folder's upkeep (its ``tidy``) without reading its status:
    what the background service runs, since nothing there shows the status.
    Kept copies have none: nothing here uses them."""
    st = State.load()
    for key, path in st.game_folders.items():
        spec = spec_for(key)
        if spec is not None:
            _tidy(spec, key, path, st)


def status(client: SyncthingClient) -> list[FolderStatus]:
    out = []
    st = State.load()
    for key, path in [*st.game_folders.items(), *st.held_folders.items()]:
        spec = spec_for(key)
        if spec is None:
            continue
        held = key in st.held_folders
        if held:  # nothing here uses it: no tidying, and the machines that play settle conflicts
            fs = FolderStatus(spec, path, stopping=stopping(key, st), held=True)
        else:
            _tidy(spec, key, path, st)
            fs = FolderStatus(spec, path, stopping=stopping(key, st),
                              conflicts=len(conflicts.find(path, spec.content)) if spec.content else 0)
        try:
            folder = client.folder_status(spec.folder_id)
            fs.state = folder.get("state")
            fs.need_items = int(folder.get("needTotalItems") or 0)
            fs.completion = client.completion(spec.folder_id).get("completion")
        except Exception:
            pass
        out.append(fs)
    return out
