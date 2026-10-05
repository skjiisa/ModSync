"""Persistent ModSync state — which MO2 instance this machine uses, and (only if
the user opted into syncing) which Syncthing folder id it is shared under.

The two are independent: an instance can be chosen without ever creating a vault,
which is all that the game-version tools and the MO2 installer need. Devices live
in Syncthing's own config (the source of truth); we only remember the local
instance path + the shared folder id so the dashboard knows what's set up.

The app and the background service each hold a copy and both write it, so a
write goes through ``update``: under a lock it reads the file, changes only the
fields given, and replaces the file whole. A process holding stale fields can't
write them back over newer ones, and a reader never sees half a file.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None

from modsync import config

_FIELDS = ("instance_path", "folder_id", "instance_label", "firewall_rules_stamp", "sync_paused",
           "copy_phase", "copy_source", "copy_expected", "copy_archive", "set_aside")


@dataclass
class State:
    instance_path: str | None = None
    folder_id: str | None = None
    instance_label: str = "Mod Organizer 2"
    # Fingerprint of the firewall rules right after ModSync added its own (see
    # modsync.firewall). Only consulted when the rules can't be read at launch.
    firewall_rules_stamp: str = ""
    # The user paused the vault. Syncthing keeps the folder and its history, so
    # resuming carries over what changed in between, deletions included.
    sync_paused: bool = False
    # A "Copy from another machine" still under way (see ModSyncService.advance_copy):
    # "receiving" while the folder is receive-only and filling up, "setting-aside"
    # once local-only files are being moved out, "" when there is none.
    copy_phase: str = ""
    copy_source: str = ""  # device id of the machine being copied
    # How far that machine's file list went when it gave out the pairing code
    # (its Syncthing sequence for the folder); 0 when it didn't say.
    copy_expected: int = 0
    copy_archive: str = ""  # where this copy keeps what it replaced or set aside
    # A finished copy's archive, while it holds anything the user may want back.
    set_aside: str = ""

    @staticmethod
    def path() -> Path:
        return config.config_dir() / "state.json"

    @classmethod
    def load(cls) -> "State":
        p = cls.path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return cls(**{k: data[k] for k in _FIELDS if k in data})
            except (OSError, ValueError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        """Write every field, replacing what is on disk. For a deliberate
        whole reset; anything else goes through ``update``."""
        with self._locked():
            self._write()

    def _write(self) -> None:
        p = self.path()
        tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, p)

    @classmethod
    def update(cls, **changes) -> "State":
        """Change only these fields on disk and return the whole state as it is now."""
        unknown = set(changes) - {f.name for f in fields(cls)}
        if unknown:
            raise TypeError(f"unknown state fields: {sorted(unknown)}")
        with cls._locked():
            state = cls.load()
            for name, value in changes.items():
                setattr(state, name, value)
            state._write()
        return state

    @classmethod
    @contextmanager
    def _locked(cls):
        p = cls.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        with open(p.with_name(p.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @property
    def has_instance(self) -> bool:
        """An MO2 instance is chosen (installed, browsed to, or joined)."""
        return bool(self.instance_path)

    @property
    def syncing(self) -> bool:
        """The instance is shared through a Syncthing vault."""
        return bool(self.instance_path and self.folder_id)

    @property
    def copying(self) -> bool:
        """A copy from another machine hasn't finished: this machine only receives."""
        return bool(self.syncing and self.copy_phase)

    @property
    def configured(self) -> bool:
        """Kept for the sync-era callers; means ``syncing``."""
        return self.syncing
