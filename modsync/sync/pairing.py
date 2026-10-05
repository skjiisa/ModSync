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
    """Register a peer device (so we'll connect to it)."""
    device = client.default_device()
    device["deviceID"] = device_id
    device["name"] = name
    if addresses:
        device["addresses"] = list(addresses)
    client.put_device(device)
    return device


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
