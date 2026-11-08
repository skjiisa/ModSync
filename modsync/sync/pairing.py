"""Pairing + folder-sharing helpers on top of the REST client.

A ModSync "vault" is one Syncthing folder (a stable folder id) = one MO2 instance,
shared between this device and its peers. Each device points the same folder id at
its own local instance path, and excludes ModOrganizer.ini via .stignore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from modsync.sync import stignore
from modsync.sync.api import SyncthingClient

DEFAULT_FOLDER_LABEL = "Mod Organizer 2 (ModSync)"


SYNC_PORT = 22000  # Syncthing's default listen port; ModSync doesn't change it


def static_addresses(host: str | None) -> list[str] | None:
    """Device addresses for a peer we've just reached at ``host`` over the LAN:
    dial it directly, and fall back to Syncthing's own discovery ("dynamic")
    if the address changes later. ``None`` means leave the default alone."""
    if not host:
        return None
    return [f"tcp://{host}:{SYNC_PORT}", "dynamic"]


def add_peer_device(
    client: SyncthingClient,
    device_id: str,
    name: str,
    addresses: Iterable[str] | None = None,
) -> dict:
    """Register a peer device (so we'll connect to it), as an introducer."""
    device = client.default_device()
    device["deviceID"] = device_id
    device["name"] = name
    if addresses:
        device["addresses"] = list(addresses)
    device.update(INTRODUCER)
    client.put_device(device)
    return device


# Names older versions gave a paired machine instead of its own.
STAND_IN_NAMES = ("ModSync device", "ModSync peer", "ModSync", "Mod Organizer 2")


def clear_stand_in_names(client: SyncthingClient, more: Iterable[str] = ()) -> list[str]:
    """Clear the names older versions made up for paired machines, so
    Syncthing fills in each machine's own the next time it connects (it does
    for a device with no name). Those were a pairing code's label, which is a
    folder's (the vault's "Skyrim SE"), the defaults in ``STAND_IN_NAMES``,
    or, on the machine that showed the LAN PIN, its own name. Any other name,
    such as one given in Syncthing, stays. A machine connected now is
    reconnected to pick up its name. Returns the ids cleared."""
    me = client.my_id()
    devices = client.devices()
    own = next((d.get("name") or "" for d in devices if d.get("deviceID") == me), "")
    stand_ins = {*STAND_IN_NAMES, *(f.get("label") or "" for f in client.folders()), own, *more} - {""}
    try:
        conns = client.connections().get("connections", {})
    except Exception:
        conns = {}
    cleared = []
    for device in devices:
        did = device.get("deviceID")
        if did in (None, "", me) or device.get("name") not in stand_ins:
            continue
        device["name"] = ""
        if (conns.get(did) or {}).get("connected") and not device.get("paused"):
            client.put_device({**device, "paused": True})  # Syncthing reads the name as it connects
        client.put_device(device)
        cleared.append(did)
    return cleared


# Every paired machine introduces the machines it shares folders with: when
# B and C have each paired with A, A tells B about C and C about B, and from
# then on they sync with each other directly, with A offline. Syncthing
# carries over the addresses A has for them. A machine that stops sharing a
# folder with A, or that A forgets, stays paired here.
INTRODUCER = {"introducer": True, "skipIntroductionRemovals": True}


def introduce_peers(client: SyncthingClient) -> list[str]:
    """Make every paired machine an introducer, for machines paired before
    ModSync did that. Returns the ids changed.

    Syncthing reads what an introducer passes on only as it connects, so a
    machine connected already is disconnected and connected again: pausing
    it and resuming it does that. One the user paused stays paused."""
    me = client.my_id()
    changed = []
    for device in client.devices():
        if device.get("deviceID") in (None, "", me) or all(device.get(k) == v for k, v in INTRODUCER.items()):
            continue
        device.update(INTRODUCER)
        if device.get("paused"):
            client.put_device(device)
        else:
            client.put_device({**device, "paused": True})
            client.put_device(device)
        changed.append(device["deviceID"])
    return changed


def share_instance_folder(
    client: SyncthingClient,
    folder_id: str,
    instance_path: Path | str,
    peer_device_ids: Iterable[str] = (),
    label: str = DEFAULT_FOLDER_LABEL,
    write_ignore: bool = True,
    *,
    receive_only: bool = False,
    versions_dir: Path | str | None = None,
) -> dict:
    """Create/replace the shared folder for an MO2 instance and write its .stignore.

    ``receive_only`` must be set in this first PUT: files Syncthing scans while
    the folder is send-and-receive are announced to peers as ordinary changes,
    and switching the type afterwards doesn't take them back. ``versions_dir``
    keeps every file Syncthing replaces or removes there (trash-can versioning,
    never cleaned out)."""
    instance_path = Path(instance_path)
    if write_ignore:
        stignore.write_stignore(instance_path)

    self_id = client.my_id()
    folder = client.default_folder()
    folder["id"] = folder_id
    folder["label"] = label
    folder["path"] = str(instance_path)
    if receive_only:
        folder["type"] = "receiveonly"
    if versions_dir is not None:
        folder["versioning"] = trashcan_versioning(versions_dir)
    device_ids = list(dict.fromkeys([self_id, *peer_device_ids]))
    folder["devices"] = [
        {"deviceID": d, "introducedBy": "", "encryptionPassword": ""}
        for d in device_ids
    ]
    client.put_folder(folder)
    return client.get_folder(folder_id)


def trashcan_versioning(versions_dir: Path | str) -> dict:
    return {
        "type": "trashcan",
        "params": {"cleanoutDays": "0"},
        "cleanupIntervalS": 3600,
        "fsPath": str(versions_dir),
        "fsType": "basic",
    }


NO_VERSIONING = {"type": "", "params": {}, "cleanupIntervalS": 3600, "fsPath": "", "fsType": "basic"}
