"""High-level ModSync operations the GUI calls.

Remembers which MO2 instance this machine uses, reports and fixes the game's
runtime version, and — only once the user opts in — owns the Syncthing daemon
(one dedicated instance) that shares the instance as a vault. Nothing here starts
Syncthing unless a sync operation needs it. All methods are synchronous/blocking —
the GUI runs them on a worker thread.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from modsync import config, gameversion, pairing_lan, platforms, skse
from modsync.downgrade import engine, recipe
from modsync.games import SKYRIM_SE
from modsync.mo2.launch import Launcher, build_plan
from modsync.pairing_code import PairingCode
from modsync.state import State
from modsync.steam import appinfo, libraries as libs, prefixes, shortcuts
from modsync.steam.appmanifest import AppManifest, PinChange
from modsync.sync import pairing, stignore
from modsync.sync.manager import SyncthingManager

log = logging.getLogger(__name__)


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


@dataclass
class GameStatus:
    """Everything the dashboard/CLI need to talk about the game's version."""

    installed: gameversion.GameVersion | None
    expected: gameversion.GameVersion | None  # what the setup's record says
    game_dir: Path | None
    language: str
    steam_public_build: int | None
    steam_is_current: bool | None  # does Steam think the install is up to date?
    steam_running: bool
    pending_pin: bool
    recipe_from: str | None
    steam_updating: bool = False  # Steam is mid-download/commit; nothing here is final
    recipe_targets: list[str] = field(default_factory=list)
    recipe_origin: str = ""
    skse_runtime: gameversion.GameVersion | None = None  # what the installed SKSE is built for
    skse_runtimes: list[str] = field(default_factory=list)  # all of them, when DLLs for several exist
    skse_source: str = ""  # e.g. "skse64_1_6_1170.dll in the game folder"
    backup_present: bool = False  # a downgrade left the originals in .modsync-downgrade/backup
    backup_stale: bool = False  # ...but the game folder already holds those files again (Steam re-installed)
    backup_from: str | None = None  # the game version the backup came from, per its manifest
    backup_bytes: int = 0
    pinned_by_modsync: bool = False  # a pin record exists, so 'unpin' can put the manifest back

    @property
    def mismatch(self) -> bool:
        return self.installed is not None and self.expected is not None and self.installed != self.expected

    @property
    def wanted(self) -> gameversion.GameVersion | None:
        """The runtime this setup should be on: the vault's record when it has
        one, otherwise what the installed SKSE was built for."""
        return self.expected if self.expected is not None else self.skse_runtime

    @property
    def wanted_from(self) -> str:
        """"vault" | "skse" | "" — where ``wanted`` came from."""
        if self.expected is not None:
            return "vault"
        return "skse" if self.skse_runtime is not None else ""

    @property
    def needs_downgrade(self) -> bool:
        return not self.steam_updating and self.installed is not None and self.wanted is not None and self.installed != self.wanted

    @property
    def can_downgrade_to(self) -> list[str]:
        """Targets the current recipe can reach from what's installed."""
        if self.installed is None or self.recipe_from is None or str(self.installed) != self.recipe_from:
            return []
        return [t for t in self.recipe_targets if t != str(self.installed)]

    @property
    def suggested_target(self) -> str | None:
        """The version to downgrade to, if the recipe can get there from here:
        the vault's, or failing that the one the installed SKSE is built for."""
        if self.needs_downgrade and str(self.wanted) in self.can_downgrade_to:
            return str(self.wanted)
        return None

    @property
    def skse_state(self) -> str:
        """How the SKSE in the game folder relates to the installed game:
        "ok" | "missing" | "wrong" (built for another runtime) | "several" | "".
        Empty while the game version is unknown, Steam is mid-update, or the
        game itself still needs switching — SKSE is the step *after* that."""
        if self.installed is None or self.steam_updating or self.needs_downgrade:
            return ""
        if self.skse_runtimes and len(self.skse_runtimes) > 1:
            return "several"
        if self.skse_runtime is None:
            return "missing"
        return "ok" if self.skse_runtime == self.installed else "wrong"

    @property
    def skse_build(self) -> skse.SkseBuild | None:
        """The SKSE build to install here, when one is needed and known."""
        if self.skse_state in ("", "ok"):
            return None
        return skse.build_for(self.installed)

    @property
    def needs_pin(self) -> bool:
        """Steam wants to update; pinning would keep the installed files."""
        return self.steam_is_current is False and not self.steam_updating

    @property
    def can_unpin(self) -> bool:
        """A ModSync pin is in effect that unpinning would reverse."""
        return self.pinned_by_modsync and self.steam_is_current is True and not self.pending_pin


@dataclass
class PinOutcome:
    applied: bool
    queued: bool
    changes: list[PinChange]
    message: str


class ModSyncService:
    def __init__(self, manager: SyncthingManager | None = None) -> None:
        self.state = State.load()
        self.launcher = Launcher()
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
        if not self.state.syncing or not path or not Path(path).is_dir():
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

    # --- the MO2 instance (no sync involved) ---
    def choose_instance(self, instance_path: Path | str, label: str | None = None) -> None:
        """Use this MO2 instance on this machine. Does not touch Syncthing.

        If the instance has no game-version record yet, one is made now (from the
        SKSE that sits next to it when possible), so the dashboard can offer the
        matching downgrade straight away."""
        if self.state.syncing and str(instance_path) != self.state.instance_path:
            raise RuntimeError("stop syncing before switching to a different instance")
        instance_path = Path(instance_path)
        log.info("using MO2 instance %s", instance_path)
        self.state.instance_path = str(instance_path)
        self.state.instance_label = label or instance_path.name or "Mod Organizer 2"
        self.state.save()
        if gameversion.VaultMeta.load(instance_path) is None:
            self.record_initial_vault_version()

    def launch_mo2(self, *, play: bool = False) -> str:
        plan = build_plan(self.state.instance_path, play=play)
        return self.launcher.start(plan)

    def forget_instance(self) -> None:
        """Stop using the chosen instance (and its vault, if any). Files stay."""
        if self.state.syncing:
            self.stop_sync()
        self.state = State()
        self.state.save()

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
        log.info("created vault %s for %s", folder_id, instance_path)
        # The creating machine defines which game runtime the vault is built for;
        # joiners receive this file through sync and compare against it.
        if gameversion.VaultMeta.load(instance_path) is None:
            self.record_initial_vault_version()
        return PairingCode(device_id, folder_id, label)

    def join_vault(
        self, code: PairingCode, instance_path: Path | str, *, peer_host: str | None = None
    ) -> PairingCode:
        """Join a vault advertised by another machine's pairing code.

        ``peer_host`` is the address the peer was reached on during LAN pairing,
        so Syncthing can connect without its own discovery."""
        self.ensure_running()
        instance_path = Path(instance_path)
        label = code.label or self.state.instance_label
        # The vault's game-version record comes from the machine we're copying;
        # anything written here before joining (choose_instance records the
        # local runtime) would be newer and win Syncthing's conflict resolution,
        # overwriting the real one on every machine.
        local_meta = gameversion.VaultMeta.path(instance_path)
        if local_meta.exists():
            log.info("dropping local %s before joining; the vault's copy wins", local_meta.name)
            local_meta.unlink()
        with self.manager.client() as client:
            pairing.add_peer_device(
                client,
                code.device_id,
                label or "ModSync device",
                addresses=pairing.static_addresses(peer_host),
            )
            pairing.share_instance_folder(
                client, code.folder_id, instance_path, [code.device_id], label=label
            )
            device_id = client.my_id()
        self._remember(instance_path, code.folder_id, label)
        log.info("joined vault %s from device %s… into %s", code.folder_id, code.device_id[:7], instance_path)
        return PairingCode(device_id, code.folder_id, label)

    def add_peer(self, code: PairingCode, *, peer_host: str | None = None) -> None:
        """Add another machine to the vault this machine already has."""
        self.ensure_running()
        if not self.state.folder_id:
            raise RuntimeError("no vault configured on this machine yet")
        with self.manager.client() as client:
            pairing.add_peer_device(
                client,
                code.device_id,
                code.label or "ModSync device",
                addresses=pairing.static_addresses(peer_host),
            )
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
                log.info("accepted pending device %s… (%s)", device_id[:7], name)
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
        self.add_peer(
            PairingCode(peer.device_id, self.state.folder_id, peer.label or name),
            peer_host=peer.host,
        )
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
        self.join_vault(
            PairingCode(peer.device_id, peer.folder_id, peer.label),
            instance_path,
            peer_host=peer.host,
        )
        return peer

    # --- undo ---
    def stop_sync(self, *, forget_devices: bool = True) -> None:
        """Leave the vault but keep using the instance on this machine.

        Stops syncing the folder and drops paired devices. **Your mods are never
        touched** — removing a Syncthing folder only stops syncing it; every file
        stays on disk.
        """
        folder_id = self.state.folder_id
        log.info("leaving vault %s (forget devices: %s)", folder_id, forget_devices)
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
        self.state.folder_id = None
        self.state.save()

    def reset(self, *, forget_devices: bool = True) -> None:
        """Forget everything on this machine: the vault and the chosen instance."""
        self.stop_sync(forget_devices=forget_devices)
        self.forget_instance()

    def my_pairing_code(self) -> PairingCode | None:
        if not self.state.syncing:
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

    def record_initial_vault_version(self) -> gameversion.VaultMeta | None:
        """First record for a brand-new vault. An imported MO2 setup was built
        for the SKSE that sits next to it, which may be older than the game
        Steam has patched to since; prefer SKSE's answer when it is unambiguous,
        so the dashboard immediately offers the right downgrade."""
        if not self.state.instance_path:
            return None
        game_dir = gameversion.find_game_dir(self.state.instance_path)
        installed = gameversion.installed_version(game_dir) if game_dir else None
        skse = gameversion.scan_skse(game_dir, self.state.instance_path)
        version, source = gameversion.choose_vault_version(installed, skse)
        if version is None:
            return None
        return gameversion.record_vault_version(self.state.instance_path, version, source=source)

    # --- game downgrade / Steam pinning ---
    def _steam_app(self):
        plat = platforms.current()
        libraries = libs.all_libraries(plat.steam_roots())
        app = libs.find_app(libraries, SKYRIM_SE.appid)
        if app is None:
            return None, None, None
        acf = app.library.steamapps / f"appmanifest_{SKYRIM_SE.appid}.acf"
        root = self._steam_root_for(app.library.path, plat.steam_roots())
        return app, acf, (appinfo.appinfo_path(root) if root else None)

    @staticmethod
    def _steam_root_for(library_path: Path, roots: list[Path]) -> Path | None:
        # appinfo.vdf lives under the Steam *root*, not under every library.
        for r in roots:
            if appinfo.appinfo_path(r).exists():
                return r
        return None

    @staticmethod
    def _pending_pin_path() -> Path:
        return config.config_dir() / "pending-pin.json"

    @staticmethod
    def _pin_record_path() -> Path:
        """What the last pin changed, so ``unpin_game_version`` can put it back."""
        return config.config_dir() / "pin-record.json"

    def game_status(self, *, refresh_index: bool = True) -> GameStatus:
        app, acf, appinfo_path = self._steam_app()
        vc = gameversion.check(self.state.instance_path)
        language = "english"
        steam_current: bool | None = None
        public_build: int | None = None
        updating = False
        if app and acf and acf.exists():
            try:
                manifest = AppManifest.load(acf)
                language = manifest.language
                updating = manifest.update_in_progress
                if appinfo_path and appinfo_path.exists():
                    info = appinfo.read_app(appinfo_path, SKYRIM_SE.appid)
                    if info:
                        public_build = info.public_buildid
                        steam_current = manifest.is_current(info)
            except (OSError, ValueError, appinfo.AppInfoError):
                pass
        try:
            idx = recipe.load_index(refresh=refresh_index)
            recipe_from, targets, origin = idx.from_version, idx.targets, idx.origin
        except Exception:
            recipe_from, targets, origin = None, [], "unavailable"
        return GameStatus(
            installed=vc.installed,
            expected=vc.expected,
            game_dir=app.install_path if app else vc.game_dir,
            language=language,
            steam_public_build=public_build,
            steam_is_current=steam_current,
            steam_running=shortcuts.steam_is_running(),
            pending_pin=self._pending_pin_path().exists(),
            steam_updating=updating,
            recipe_from=recipe_from,
            recipe_targets=targets,
            recipe_origin=origin,
            skse_runtime=vc.skse.runtime,
            skse_runtimes=[str(v) for v in vc.skse.runtimes],
            skse_source=vc.skse.describe(),
            pinned_by_modsync=self._pin_record_path().exists(),
            **self._backup_fields(app.install_path if app else vc.game_dir),
        )

    @staticmethod
    def _backup_fields(game_dir: Path | None) -> dict:
        if not engine.has_backup(game_dir):
            return {}
        assert game_dir is not None
        manifest = engine._read_manifest(engine.work_dir_for(game_dir))
        return {
            "backup_present": True,
            "backup_stale": engine.backup_is_stale(game_dir),
            "backup_from": str(manifest["from_version"]) if manifest.get("from_version") else None,
            "backup_bytes": engine.backup_size(game_dir),
        }

    def plan_downgrade(self, target: str, *, refresh_index: bool = True) -> engine.Plan:
        app, acf, _ = self._steam_app()
        if app is None:
            raise engine.DowngradeError(f"{SKYRIM_SE.name} is not installed through Steam on this machine")
        language = "english"
        if acf and acf.exists():
            try:
                language = AppManifest.load(acf).language
            except (OSError, ValueError):
                pass
        idx = recipe.load_index(refresh=refresh_index)
        return engine.make_plan(idx, app.install_path, target, language)

    def run_downgrade(self, target: str, progress: engine.ProgressFn | None = None) -> engine.Result:
        """Download the community patches and rewrite the game files in place.
        Steam's manifest is left alone: right after a Steam update it already
        claims the current build, so the game launches from Steam as-is."""
        app, _, _ = self._steam_app()
        plan = self.plan_downgrade(target)
        prefix = prefixes.compat_prefix(app.library, SKYRIM_SE.appid) if app else None
        cache = config.data_dir() / "downgrade" / "cache"
        log.info("downgrade %s -> %s (%s) in %s: %d archive(s)", plan.from_version, plan.target, plan.language,
                 plan.game_dir, len(plan.archives))
        try:
            result = engine.run(plan, cache_dir=cache, prefix_dir=prefix, progress=progress)
        except Exception:
            log.exception("downgrade to %s failed", target)
            raise
        log.info("downgrade finished: game reports %s, %d files patched, %d bytes downloaded",
                 result.installed_version, len(result.patched_files), result.downloaded_bytes)
        return result

    def install_skse(self, progress: engine.ProgressFn | None = None) -> skse.Installed:
        """Put the SKSE build for the *installed* game version into the game
        folder, replacing any other SKSE there. Blocks."""
        st = self.game_status(refresh_index=False)
        if st.installed is None or st.game_dir is None:
            raise RuntimeError("could not find the installed game")
        build = skse.build_for(st.installed)
        if build is None:
            raise RuntimeError(f"ModSync doesn't know an SKSE build for Skyrim {st.installed}; see {skse.SKSE_PAGE}")
        log.info("installing SKSE %s for %s into %s", build.version, build.runtime, st.game_dir)
        return skse.install(build, st.game_dir, config.data_dir() / "skse", progress)

    def restore_game_files(self, progress: engine.ProgressFn | None = None) -> engine.RestoreResult:
        """Undo a downgrade: move the backed-up originals back into the game
        folder and drop the backup. Steam's manifest is left alone; if it was
        pinned, the files it now describes really are the current build."""
        result = engine.restore(self._game_dir_for_backup(), progress)
        log.info("restored %d original file(s) into %s (%d mismatch(es))",
                 len(result.restored), result.game_dir, len(result.mismatches))
        return result

    def discard_downgrade_backup(self) -> int:
        """Delete a leftover backup without restoring it. Returns bytes freed."""
        freed = engine.discard_backup(self._game_dir_for_backup())
        log.info("discarded the downgrade backup (%d bytes freed)", freed)
        return freed

    def _game_dir_for_backup(self) -> Path:
        app, _, _ = self._steam_app()
        game_dir = app.install_path if app else gameversion.find_game_dir(self.state.instance_path)
        if game_dir is None:
            raise engine.DowngradeError(f"{SKYRIM_SE.name} was not found on this machine")
        return Path(game_dir)

    def pin_game_version(self, *, queue_if_steam_running: bool = True) -> PinOutcome:
        """Make Steam consider the installed files current so it launches the
        game without updating. Needs Steam closed; otherwise (optionally) queue
        it for the background service to apply the moment Steam exits."""
        app, acf, appinfo_path = self._steam_app()
        if not app or not acf or not acf.exists():
            raise RuntimeError(f"{SKYRIM_SE.name} is not installed through Steam on this machine")
        if not appinfo_path or not appinfo_path.exists():
            raise RuntimeError("Steam's product cache (appinfo.vdf) was not found")
        if shortcuts.steam_is_running():
            if not queue_if_steam_running:
                raise RuntimeError("Close Steam first — it rewrites the appmanifest while running")
            self._queue_pin()
            log.info("Steam is running; pin queued for when it exits")
            return PinOutcome(
                applied=False,
                queued=True,
                changes=[],
                message=(
                    "Steam is running, so the pin is queued. Restart Steam (on the Deck: "
                    "Power menu → Restart Steam) and ModSync's background service will "
                    "apply it while Steam is closed."
                ),
            )
        info = appinfo.read_app(appinfo_path, SKYRIM_SE.appid)
        if info is None or info.public_buildid is None:
            raise RuntimeError("Steam's product cache has no current build for this game yet")
        manifest = AppManifest.load(acf)
        changes = manifest.pin_to(info)
        if changes:
            manifest.save()
            log.info("pinned %s to build %s: %s", acf.name, info.public_buildid,
                     ", ".join(f"{c.field} {c.old}->{c.new}" for c in changes))
            self._save_pin_record(changes)
        self._pending_pin_path().unlink(missing_ok=True)
        msg = (
            f"Pinned: Steam now treats the installed files as build {info.public_buildid}."
            if changes
            else "Already pinned — Steam considers this install up to date."
        )
        return PinOutcome(applied=bool(changes), queued=False, changes=changes, message=msg)

    def _save_pin_record(self, changes: list[PinChange]) -> None:
        p = self._pin_record_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {
                    "appid": SKYRIM_SE.appid,
                    "pinned_at": datetime.now(timezone.utc).isoformat(),
                    "changes": [{"field": c.field, "old": c.old, "new": c.new} for c in changes],
                }
            ),
            encoding="utf-8",
        )

    def _load_pin_record(self) -> list[PinChange] | None:
        try:
            doc = json.loads(self._pin_record_path().read_text(encoding="utf-8"))
            return [PinChange(str(c["field"]), c.get("old"), str(c["new"])) for c in doc["changes"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def unpin_game_version(self) -> PinOutcome:
        """Let Steam update the game again: put the manifest fields the pin
        changed back (or, without a record, flag the install as needing an
        update). Also drops any queued pin. Needs Steam closed."""
        app, acf, _ = self._steam_app()
        if not app or not acf or not acf.exists():
            raise RuntimeError(f"{SKYRIM_SE.name} is not installed through Steam on this machine")
        if shortcuts.steam_is_running():
            raise RuntimeError("Close Steam first — it rewrites the appmanifest while running")
        record = self._load_pin_record()
        manifest = AppManifest.load(acf)
        changes = manifest.unpin(record)
        if changes:
            manifest.save()
            log.info("unpinned %s (%s record): %s", acf.name, "with" if record else "no",
                     ", ".join(f"{c.field} {c.old}->{c.new}" for c in changes))
        self._pending_pin_path().unlink(missing_ok=True)
        self._pin_record_path().unlink(missing_ok=True)
        if changes and record:
            msg = "Unpinned: Steam's manifest is back to what it said before the pin, so Steam will update the game again."
        elif changes:
            msg = (
                "Unpinned: Steam now sees this install as needing an update and will re-check it on its "
                "next launch. If it does not update, use \"Verify integrity of game files\" in Steam."
            )
        else:
            msg = "Nothing to unpin — Steam's manifest does not carry a ModSync pin."
        return PinOutcome(applied=bool(changes), queued=False, changes=changes, message=msg)

    def _queue_pin(self) -> None:
        p = self._pending_pin_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"appid": SKYRIM_SE.appid, "requested_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )

    def apply_pending_pin(self) -> PinOutcome | None:
        """Called periodically by the serve loop / dashboard: apply a queued
        pin once Steam has exited. Steam rewrites appmanifests on shutdown, so
        wait a moment after it disappears before touching the file."""
        if not self._pending_pin_path().exists() or shortcuts.steam_is_running():
            return None
        time.sleep(2.0)
        if shortcuts.steam_is_running():
            return None
        try:
            return self.pin_game_version(queue_if_steam_running=False)
        except Exception as exc:
            log.error("queued pin failed: %s", exc)
            return PinOutcome(False, True, [], f"pin failed: {exc}")

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
                configured=self.state.syncing,
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
