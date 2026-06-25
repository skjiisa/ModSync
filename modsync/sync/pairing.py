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
) -> dict:
    """Create/replace the shared folder for an MO2 instance and write its .stignore."""
    instance_path = Path(instance_path)
    if write_ignore:
        stignore.write_stignore(instance_path)

    self_id = client.my_id()
    folder = client.default_folder()
    folder["id"] = folder_id
    folder["label"] = label
    folder["path"] = str(instance_path)
    device_ids = list(dict.fromkeys([self_id, *peer_device_ids]))
    folder["devices"] = [
        {"deviceID": d, "introducedBy": "", "encryptionPassword": ""}
        for d in device_ids
    ]
    client.put_folder(folder)
    return client.get_folder(folder_id)
