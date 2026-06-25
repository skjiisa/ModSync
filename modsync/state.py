"""Persistent ModSync state — which instance is synced under which vault (folder id).

Devices live in Syncthing's own config (the source of truth); we only remember the
local instance path + the shared folder id so the dashboard knows what's set up.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from modsync import config

_FIELDS = ("instance_path", "folder_id", "instance_label")


@dataclass
class State:
    instance_path: str | None = None
    folder_id: str | None = None
    instance_label: str = "Mod Organizer 2"

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
    def configured(self) -> bool:
        return bool(self.instance_path and self.folder_id)
