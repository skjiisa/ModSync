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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from modsync.state import State
from modsync.sync.api import SyncthingClient

log = logging.getLogger(__name__)

FOLDER_PREFIX = "modsync-"


@dataclass(frozen=True)
class FolderSpec:
    key: str  # e.g. "dfu-mods"; the folder id is "modsync-<key>"
    label: str  # how Syncthing and ModSync name it
    path: Callable[[], Path | None]  # where it is on this machine; None while the game isn't set up
    ignore: tuple[str, ...]  # its .stignore, one pattern per line

    @property
    def folder_id(self) -> str:
        return FOLDER_PREFIX + self.key

    def stignore_text(self) -> str:
        return "\n".join(self.ignore) + "\n"


def specs() -> list[FolderSpec]:
    """Every game folder ModSync knows. The games add theirs here."""
    return []


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


def device_entries(ids) -> list[dict]:
    """Syncthing's list of a folder's devices, in order, each once."""
    return [{"deviceID": d, "introducedBy": "", "encryptionPassword": ""} for d in dict.fromkeys(ids)]


def peers(client: SyncthingClient) -> list[str]:
    """Every paired machine: each Syncthing device but this one."""
    me = client.my_id()
    return [d["deviceID"] for d in client.devices() if d.get("deviceID") and d["deviceID"] != me]


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


def _put(client: SyncthingClient, spec: FolderSpec, root: Path, device_ids: list[str]) -> None:
    """Create the folder, or add machines to it as it is: its other settings
    (paused, versioning, ...) and the machines it already has stay."""
    _prepare(spec, root)
    try:
        folder = client.get_folder(spec.folder_id)
    except Exception:
        folder = client.default_folder()
        folder["id"] = spec.folder_id
        folder["label"] = spec.label
    folder["path"] = str(root)
    have = [d["deviceID"] for d in folder.get("devices", [])]
    folder["devices"] = device_entries([client.my_id(), *have, *device_ids])
    client.put_folder(folder)


def _remember(key: str, path: Path) -> None:
    st = State.load()
    State.update(game_folders={**st.game_folders, key: str(path)},
                 declined_folders=[k for k in st.declined_folders if k != key])


def share(client: SyncthingClient, spec: FolderSpec) -> Path:
    """Start syncing ``spec`` from this machine, with every paired machine."""
    root = spec.path()
    if root is None:
        raise RuntimeError(f"{spec.label}: set the game up on this machine first.")
    _put(client, spec, root, peers(client))
    _remember(spec.key, root)
    log.info("sharing %s (%s) from %s", spec.folder_id, spec.label, root)
    return root


def stop(client: SyncthingClient | None, spec: FolderSpec) -> None:
    """Stop syncing ``spec`` here. Files stay on disk, and offers of it from
    other machines are no longer taken up."""
    if client is not None:
        try:
            client.delete_folder(spec.folder_id)
        except Exception as exc:  # gone already, or the daemon is down: our own record is what matters
            log.info("could not remove %s from Syncthing: %s", spec.folder_id, exc)
    st = State.load()
    State.update(game_folders={k: v for k, v in st.game_folders.items() if k != spec.key},
                 declined_folders=sorted({*st.declined_folders, spec.key}))
    log.info("stopped syncing %s", spec.folder_id)


def share_with(client: SyncthingClient, device_id: str) -> list[str]:
    """Add a newly paired machine to every game folder synced here."""
    added = []
    for key in State.load().game_folders:
        spec = spec_for(key)
        if spec is None:
            continue
        try:
            folder = client.get_folder(spec.folder_id)
        except Exception:
            continue  # not in Syncthing (removed by hand): take_offers or share puts it back
        ids = [d["deviceID"] for d in folder.get("devices", [])]
        if device_id in ids:
            continue
        folder["devices"] = device_entries([*ids, device_id])
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
    paired = set(peers(client))
    taken = []
    for folder_id, info in pending.items():
        spec = _spec_for_id(folder_id)
        if spec is None or spec.key in st.declined_folders or spec.key in st.game_folders:
            continue
        offered_by = [d for d in ((info or {}).get("offeredBy") or {}) if d in paired]
        root = spec.path()
        if not offered_by or root is None:
            continue
        _put(client, spec, root, sorted(paired))
        _remember(spec.key, root)
        taken.append(spec.key)
        log.info("took up %s from %s at %s", folder_id, ", ".join(d[:7] for d in offered_by), root)
    return taken


def refresh(client: SyncthingClient) -> None:
    """Keep each synced folder's .stignore current with this version of ModSync,
    and follow a game that moved on this machine (say, reinstalled elsewhere)."""
    for key, recorded in State.load().game_folders.items():
        spec = spec_for(key)
        root = spec.path() if spec else None
        if spec is None or root is None:
            continue
        if str(root) != recorded:
            # Pointing the folder at the new place would read every file not
            # there yet as deleted, and send those deletions to the others.
            # Removed and added again, it starts with no record here: files
            # the others have arrive, and nothing is deleted anywhere.
            try:
                _prepare(spec, root)  # before anything is removed
                folder = client.get_folder(spec.folder_id)
            except Exception as exc:
                log.warning("%s moved to %s, but it can't be set up there yet: %s", spec.folder_id, root, exc)
                continue
            ids = [d["deviceID"] for d in folder.get("devices", [])]
            fresh = {**client.default_folder(), "id": spec.folder_id, "label": folder.get("label") or spec.label,
                     "path": str(root), "devices": device_entries(ids)}
            client.delete_folder(spec.folder_id)
            try:
                client.put_folder(fresh)
            except Exception:
                client.put_folder(folder)  # put the old one back, to try again next time
                raise
            _remember(key, root)
            log.info("%s moved from %s to %s", spec.folder_id, recorded, root)
            continue
        try:
            write_stignore(spec, root)
        except OSError:
            pass


def status(client: SyncthingClient) -> list[FolderStatus]:
    out = []
    for key, path in State.load().game_folders.items():
        spec = spec_for(key)
        if spec is None:
            continue
        fs = FolderStatus(spec, path)
        try:
            folder = client.folder_status(spec.folder_id)
            fs.state = folder.get("state")
            fs.need_items = int(folder.get("needTotalItems") or 0)
            fs.completion = client.completion(spec.folder_id).get("completion")
        except Exception:
            pass
        out.append(fs)
    return out
