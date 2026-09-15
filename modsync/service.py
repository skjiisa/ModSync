"""High-level ModSync operations the GUI calls.

Owns the Syncthing daemon (one dedicated instance), persists which MO2 instance is
synced under which vault, and exposes create/join/add-peer/status. All methods are
synchronous/blocking — the GUI runs them on a worker thread.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from modsync import config, gameversion, pairing_lan
from modsync.pairing_code import PairingCode
from modsync.state import State
from modsync.sync import pairing, stignore
from modsync.sync.manager import SyncthingManager


@dataclass
class DeviceStatus:
    id: str
    name: str
    connected: bool


@dataclass
class SyncStatus:
    device_id: str
    folder_id: str | None
    configured: bool
    folder_state: str | None
    completion: float | None
    devices: list[DeviceStatus]


class ModSyncService:
    def __init__(self, manager: SyncthingManager | None = None) -> None:
        self.state = State.load()
        self.manager = manager or SyncthingManager(
            home=config.syncthing_home(),
            log_file=config.data_dir() / "syncthing.log",
        )

    # --- lifecycle ---
    def ensure_running(self, timeout: float = 40.0) -> None:
        if not self.manager.running:
            self.manager.start(timeout=timeout)
        self._refresh_stignore()

    def _refresh_stignore(self) -> None:
        """Keep an existing vault's .stignore current with this version of ModSync
        (e.g. so a vault created before the game-version record whitelisted
        ``modsync-vault.json`` starts syncing it). Syncthing watches the file, so
        no restart is needed. Only rewrites when the content actually differs."""
        path = self.state.instance_path
        if not self.state.configured or not path or not Path(path).is_dir():
            return
        target = Path(path) / ".stignore"
        try:
            if target.read_text(encoding="utf-8") == stignore.stignore_text():
                return
        except OSError:
            pass
        try:
            stignore.write_stignore(path)
        except OSError:
            pass  # read-only instance dir etc.; not worth failing startup over

    def shutdown(self) -> None:
        self.manager.stop()

    def device_id(self) -> str:
        self.ensure_running()
        return self.manager.device_id()

    # --- vaults ---
    @staticmethod
    def _new_folder_id() -> str:
        return "modsync-" + secrets.token_hex(8)

    def create_vault(self, instance_path: Path | str, label: str = "Mod Organizer 2") -> PairingCode:
        """This machine holds the canonical setup; start a new vault for it."""
        self.ensure_running()
        instance_path = Path(instance_path)
        folder_id = self.state.folder_id or self._new_folder_id()
        with self.manager.client() as client:
            pairing.share_instance_folder(client, folder_id, instance_path, [], label=label)
            device_id = client.my_id()
        self._remember(instance_path, folder_id, label)
        # The creating machine defines which game runtime the vault is built for;
        # joiners receive this file through sync and compare against it.
        if gameversion.VaultMeta.load(instance_path) is None:
            self.adopt_local_game_version()
        return PairingCode(device_id, folder_id, label)

    def join_vault(self, code: PairingCode, instance_path: Path | str) -> PairingCode:
        """Join a vault advertised by another machine's pairing code."""
        self.ensure_running()
        instance_path = Path(instance_path)
        label = code.label or self.state.instance_label
        with self.manager.client() as client:
            pairing.add_peer_device(client, code.device_id, label or "ModSync device")
            pairing.share_instance_folder(
                client, code.folder_id, instance_path, [code.device_id], label=label
            )
            device_id = client.my_id()
        self._remember(instance_path, code.folder_id, label)
        return PairingCode(device_id, code.folder_id, label)

    def add_peer(self, code: PairingCode) -> None:
        """Add another machine to the vault this machine already has."""
        self.ensure_running()
        if not self.state.folder_id:
            raise RuntimeError("no vault configured on this machine yet")
        with self.manager.client() as client:
            pairing.add_peer_device(client, code.device_id, code.label or "ModSync device")
            folder = client.get_folder(self.state.folder_id)
            ids = {d["deviceID"] for d in folder.get("devices", [])}
            ids.add(code.device_id)
            folder["devices"] = [
                {"deviceID": d, "introducedBy": "", "encryptionPassword": ""}
                for d in ids
            ]
            client.put_folder(folder)

    def accept_pending(self) -> list[str]:
        """Add any devices that have tried to connect, sharing the vault with them.

        This is what lets the side that *created* the vault accept the side that
        *joined* it without a second round of code-pasting. Returns the accepted
        device ids. Only a device that knows our device id (i.e. has our pairing
        code) can become pending, and accepting it only grants this one vault.
        """
        if not self.state.folder_id:
            return []
        self.ensure_running()
        accepted: list[str] = []
        with self.manager.client() as client:
            pending = client.pending_devices() or {}
            for device_id, info in pending.items():
                name = (info or {}).get("name") or "ModSync peer"
                pairing.add_peer_device(client, device_id, name)
                folder = client.get_folder(self.state.folder_id)
                ids = {d["deviceID"] for d in folder.get("devices", [])}
                ids.add(device_id)
                folder["devices"] = [
                    {"deviceID": d, "introducedBy": "", "encryptionPassword": ""}
                    for d in ids
                ]
                client.put_folder(folder)
                accepted.append(device_id)
        return accepted

    # --- LAN pairing (no code typing) ---
    def host_network_pairing(
        self,
        name: str,
        pin: str,
        *,
        on_ready=None,
        stop=None,
        timeout: float = 120.0,
    ) -> pairing_lan.PairPayload:
        """Offer this machine's vault on the LAN and wait for a peer to pair with
        the PIN, then add it to the vault. Requires a vault here already. Blocks."""
        self.ensure_running()
        if not self.state.folder_id:
            raise RuntimeError("create a vault on this machine first")
        payload = pairing_lan.PairPayload(
            self.device_id(), self.state.folder_id, self.state.instance_label
        )
        peer = pairing_lan.host_pairing(
            payload, name, pin, on_ready=on_ready, stop=stop, timeout=timeout
        )
        self.add_peer(PairingCode(peer.device_id, self.state.folder_id, peer.label or name))
        return peer

    def discover_hosts(self, timeout: float = 3.0) -> list[pairing_lan.Announcement]:
        """List ModSync machines currently offering to pair on the LAN."""
        return pairing_lan.discover(timeout)

    def join_via_network(
        self,
        announcement: pairing_lan.Announcement,
        pin: str,
        instance_path: Path | str,
        *,
        timeout: float = 15.0,
    ) -> pairing_lan.PairPayload:
        """Pair with a discovered host via PIN and join its vault. Blocks."""
        self.ensure_running()
        payload = pairing_lan.PairPayload(self.device_id())
        peer = pairing_lan.join_pairing(announcement, payload, pin, timeout=timeout)
        if not peer.folder_id:
            raise RuntimeError("that machine isn't offering a vault to join")
        self.join_vault(PairingCode(peer.device_id, peer.folder_id, peer.label), instance_path)
        return peer

    # --- undo ---
    def reset(self, *, forget_devices: bool = True) -> None:
        """Forget this machine's setup so it can be set up differently.

        Stops syncing the vault folder, drops paired devices, and clears our
        state. **Your mods are never touched** — removing a Syncthing folder only
        stops syncing it; every file stays on disk.
        """
        folder_id = self.state.folder_id
        try:
            self.ensure_running()
            with self.manager.client() as client:
                if folder_id:
                    try:
                        client.delete_folder(folder_id)
                    except Exception:
                        pass
                if forget_devices:
                    me = client.my_id()
                    for dev in client.devices():
                        did = dev.get("deviceID")
                        if did and did != me:
                            try:
                                client.delete_device(did)
                            except Exception:
                                pass
        except Exception:
            pass  # daemon may be down; clearing our own state is what matters
        self.state = State()
        self.state.save()

    def my_pairing_code(self) -> PairingCode | None:
        if not self.state.configured or not self.state.folder_id:
            return None
        return PairingCode(self.device_id(), self.state.folder_id, self.state.instance_label)

    def rescan(self) -> None:
        if not self.state.folder_id:
            return
        self.ensure_running()
        with self.manager.client() as client:
            client.rescan(self.state.folder_id)

    # --- game runtime version ---
    def game_version_check(self) -> gameversion.VersionCheck:
        """Compare the game runtime installed here with the one the vault records."""
        return gameversion.check(self.state.instance_path)

    def adopt_local_game_version(self) -> gameversion.VaultMeta | None:
        """Record this machine's installed runtime as the vault's expected version.

        Used when the vault is created, and explicitly by the user after they
        upgrade or downgrade the game on purpose. Returns None if there is no
        instance or the runtime cannot be detected."""
        if not self.state.instance_path:
            return None
        game_dir = gameversion.find_game_dir(self.state.instance_path)
        installed = gameversion.installed_version(game_dir) if game_dir else None
        if installed is None:
            return None
        return gameversion.record_vault_version(self.state.instance_path, installed)

    # --- status ---
    def status(self) -> SyncStatus:
        self.ensure_running()
        with self.manager.client() as client:
            me = client.my_id()
            conns = client.connections().get("connections", {})
            devices: list[DeviceStatus] = []
            for d in client.devices():
                did = d["deviceID"]
                if did == me:
                    continue
                devices.append(
                    DeviceStatus(
                        id=did,
                        name=d.get("name", ""),
                        connected=bool(conns.get(did, {}).get("connected")),
                    )
                )
            folder_state = None
            completion = None
            if self.state.folder_id:
                try:
                    folder_state = client.folder_status(self.state.folder_id).get("state")
                    completion = client.completion(self.state.folder_id).get("completion")
                except Exception:
                    pass
            return SyncStatus(
                device_id=me,
                folder_id=self.state.folder_id,
                configured=self.state.configured,
                folder_state=folder_state,
                completion=completion,
                devices=devices,
            )

    # --- internal ---
    def _remember(self, instance_path: Path, folder_id: str, label: str) -> None:
        self.state.instance_path = str(instance_path)
        self.state.folder_id = folder_id
        self.state.instance_label = label
        self.state.save()
