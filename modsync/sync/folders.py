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

Skyrim's vault stays as it was (``ModSyncService``): a random folder id that
the pairing code carries, a copy that starts receive-only, and MO2-aware
conflicts. Nothing here touches it.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from modsync.state import State
from modsync.sync import conflicts

if TYPE_CHECKING:  # `modsync doctor` reads FOLDER_PREFIX without httpx
    from modsync.sync.api import SyncthingClient

log = logging.getLogger(__name__)

FOLDER_PREFIX = "modsync-"


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

    @property
    def folder_id(self) -> str:
        return FOLDER_PREFIX + self.key

    def stignore_text(self) -> str:
        return "\n".join(self.ignore) + "\n"


def specs() -> list[FolderSpec]:
    """Every game folder ModSync knows. The games add theirs here."""
    from modsync import cyberpunk, dfu  # import this module's users; loaded on first use

    return [dfu.MODS_FOLDER, dfu.SAVES_FOLDER, cyberpunk.MODS_FOLDER]


def spec_for(key: str) -> FolderSpec | None:
    return next((s for s in specs() if s.key == key), None)


def _spec_for_id(folder_id: str) -> FolderSpec | None:
    return next((s for s in specs() if s.folder_id == folder_id), None)


@dataclass
class FolderStatus:
    spec: FolderSpec
    path: str
    state: str | None = None  # Syncthing's folder state; None when it didn't answer
    completion: float | None = None
    need_items: int = 0
    stopping: bool = False  # stopped here; Syncthing lets go of it once it answers
    conflicts: int = 0  # conflict copies waiting for a choice


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


def _put(client: SyncthingClient, spec: FolderSpec, root: Path, device_ids: list[str]) -> None:
    """Create the folder, or add machines to it as it is: its other settings
    (paused, versioning, ...) and the machines it already has stay. One that
    is configured somewhere else here is moved, never repointed."""
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


def _remember(key: str, path: Path) -> None:
    def change(st: State) -> None:
        st.game_folders = {**st.game_folders, key: str(path)}
        st.declined_folders = [k for k in st.declined_folders if k != key]

    State.change(change)


def _forget(key: str) -> None:
    def change(st: State) -> None:
        st.game_folders = {k: v for k, v in st.game_folders.items() if k != key}

    State.change(change)


def _decline(key: str) -> None:
    def change(st: State) -> None:
        st.declined_folders = sorted({*st.declined_folders, key})

    State.change(change)


def stopping(key: str, st: State | None = None) -> bool:
    """Stopped here, but not yet removed from Syncthing (it wasn't answering)."""
    st = st or State.load()
    return key in st.game_folders and key in st.declined_folders


def share(client: SyncthingClient, spec: FolderSpec) -> Path:
    """Start syncing ``spec`` from this machine, with every paired machine."""
    root = spec.path()
    if root is None:
        raise RuntimeError(f"{spec.label}: set the game up on this machine first.")
    if spec.on_share is not None and not _offered(client, spec):
        spec.on_share(root)  # a new shared folder; one already offered is joined as it is
    _put(client, spec, root, peers(client))
    _remember(spec.key, root)
    log.info("sharing %s (%s) from %s", spec.folder_id, spec.label, root)
    return root


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
    for rel in spec.content:
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
    """Syncthing has let go of it: forget it here, and let the game tidy up."""
    _forget(spec.key)
    if spec.on_stop is not None:
        try:
            spec.on_stop()
        except Exception as exc:
            log.warning("after stopping %s: %s", spec.folder_id, exc)
    log.info("stopped syncing %s", spec.folder_id)


def share_with(client: SyncthingClient, device_id: str) -> list[str]:
    """Add a newly paired machine to every game folder synced here."""
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
    for key in st.game_folders:
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
    here and the folder wasn't stopped here. Returns their keys."""
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
        if spec is None or spec.key in st.declined_folders or spec.key in st.game_folders:
            continue
        offered_by = [d for d in ((info or {}).get("offeredBy") or {}) if d in paired]
        root = spec.path()
        if not offered_by or root is None:
            continue
        _put(client, spec, root, sorted(share | set(offered_by)))
        _remember(spec.key, root)
        taken.append(spec.key)
        log.info("took up %s from %s at %s", folder_id, ", ".join(d[:7] for d in offered_by), root)
    return taken


def refresh(client: SyncthingClient) -> None:
    """Finish stopping folders Syncthing didn't let go of before, keep each
    synced folder's .stignore current with this version of ModSync, and
    follow a game that moved on this machine (say, reinstalled elsewhere)."""
    st = State.load()
    for key, recorded in st.game_folders.items():
        spec = spec_for(key)
        if spec is None:
            continue
        if stopping(key, st):
            if _remove(client, spec):
                _finish_stop(spec)
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


def status(client: SyncthingClient) -> list[FolderStatus]:
    out = []
    st = State.load()
    for key, path in st.game_folders.items():
        spec = spec_for(key)
        if spec is None:
            continue
        if spec.tidy is not None and not stopping(key, st):
            try:
                spec.tidy(Path(path))
            except Exception as exc:  # a status read must never fail over it
                log.warning("could not tidy %s: %s", spec.folder_id, exc)
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
