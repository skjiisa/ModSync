"""Persistent ModSync state — which MO2 instance this machine uses, and (only if
the user opted into syncing) which Syncthing folder id it is shared under.

The two are independent: an instance can be chosen without ever creating a vault,
which is all that the game-version tools and the MO2 installer need. Devices live
in Syncthing's own config (the source of truth); we only remember the local
instance path + the shared folder id so the dashboard knows what's set up.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from modsync import config

_FIELDS = ("instance_path", "folder_id", "instance_label", "firewall_allowed")


@dataclass
class State:
    instance_path: str | None = None
    folder_id: str | None = None
    instance_label: str = "Mod Organizer 2"
    # ModSync opened its ports in this machine's firewall (see modsync.firewall),
    # so the dashboard can stop warning about it.
    firewall_allowed: bool = False

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
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @property
    def has_instance(self) -> bool:
        """An MO2 instance is chosen (installed, browsed to, or joined)."""
        return bool(self.instance_path)

    @property
    def syncing(self) -> bool:
        """The instance is shared through a Syncthing vault."""
        return bool(self.instance_path and self.folder_id)

    @property
    def configured(self) -> bool:
        """Kept for the sync-era callers; means ``syncing``."""
        return self.syncing
