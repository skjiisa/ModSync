"""Which compatibility tool Steam runs a game with: ``config/config.vdf``.

``InstallConfigStore/Software/Valve/Steam/CompatToolMapping/<appid>`` holds the
per-game choice made under *Properties → Compatibility* (``"0"`` is the global
default). Steam keeps this file in memory and rewrites it on exit, so an edit
only sticks while Steam is **closed**; callers check that first, the same way
the appmanifest pin does. The text parser round-trips Steam's own file
byte-for-byte, and a backup is kept next to it before every write.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from modsync.steam import vdf

BACKUP_SUFFIX = ".modsync-bak"


def config_vdf_path(steam_root: Path | str) -> Path:
    return Path(steam_root) / "config" / "config.vdf"


def _ci_key(d: dict, key: str) -> str | None:
    kl = key.lower()
    for k in d:
        if k.lower() == kl:
            return k
    return None


class SteamConfig:
    def __init__(self, path: Path, data: vdf.KV) -> None:
        self.path = path
        self.data = data

    @classmethod
    def load(cls, path: Path | str) -> "SteamConfig":
        p = Path(path)
        return cls(p, vdf.load(p))

    # --- the mapping block ---------------------------------------------------
    def _steam_block(self, create: bool) -> dict | None:
        node: dict = self.data
        for name in ("InstallConfigStore", "Software", "Valve", "Steam"):
            key = _ci_key(node, name)
            if key is None or not isinstance(node.get(key), dict):
                if not create:
                    return None
                key = key or name
                node[key] = {}
            node = node[key]
        return node

    def mapping(self, create: bool = False) -> dict | None:
        steam = self._steam_block(create)
        if steam is None:
            return None
        key = _ci_key(steam, "CompatToolMapping")
        if key is None or not isinstance(steam.get(key), dict):
            if not create:
                return None
            key = key or "CompatToolMapping"
            steam[key] = {}
        return steam[key]

    def compat_tool(self, appid: int) -> dict | None:
        """The mapping entry for ``appid`` (``{"name", "config", "priority"}``), or None."""
        block = self.mapping()
        entry = block.get(str(appid)) if block else None
        return dict(entry) if isinstance(entry, dict) else None

    def compat_tool_name(self, appid: int) -> str | None:
        entry = self.compat_tool(appid)
        name = entry.get("name") if entry else None
        return str(name) if name else None

    def set_compat_tool(self, appid: int, name: str, *, config: str = "", priority: str = "250") -> None:
        block = self.mapping(create=True)
        assert block is not None
        block[str(appid)] = {"name": name, "config": config, "priority": priority}

    def set_compat_entry(self, appid: int, entry: dict | None) -> None:
        """Restore a whole entry as previously read (or remove it when None)."""
        if entry is None:
            self.remove_compat_tool(appid)
            return
        block = self.mapping(create=True)
        assert block is not None
        block[str(appid)] = {k: str(v) for k, v in entry.items()}

    def remove_compat_tool(self, appid: int) -> None:
        block = self.mapping()
        if block is not None:
            block.pop(str(appid), None)

    # --- write ---------------------------------------------------------------
    def save(self, *, backup: bool = True) -> None:
        if backup and self.path.exists():
            shutil.copy2(self.path, self.path.with_name(self.path.name + BACKUP_SUFFIX))
        tmp = self.path.with_suffix(self.path.suffix + ".modsync-tmp")
        vdf.dump(self.data, tmp)
        tmp.replace(self.path)
