"""Read and *pin* a game's ``appmanifest_<appid>.acf``.

Steam decides whether an installed game needs an update from this file alone:
``StateFlags`` (4 = fully installed, 6 = update required), ``buildid`` versus
the current public build, and the per-depot ``manifest`` ids under
``InstalledDepots``. It never hashes the game files at launch.

So after ModSync has downgraded the files, **pinning** the manifest to the
current public build (read from Steam's own product cache, see ``appinfo.py``)
makes Steam treat the game as up to date: it launches normally from Steam,
on the Deck or desktop, with no update. When Bethesda ships the next patch the
flag flips again and pinning has to be repeated — no download needed, the files
are already the wanted version.

Steam holds this file in memory and rewrites it while running, so a pin only
sticks if Steam is **not running**; callers must check that first.

**Unpinning** reverses this: given the ``PinChange`` list a pin returned, the
fields go back to their previous values and Steam wants the update again.
Without that record the manifest is flagged "update required" with an unknown
build, which makes Steam re-check (and, if needed, re-download) the game.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from modsync.steam import vdf
from modsync.steam.appinfo import AppInfo
from modsync.steam.libraries import Library

STATE_FULLY_INSTALLED = 4
STATE_UPDATE_REQUIRED = 6


@dataclass
class PinChange:
    field: str
    old: str | None
    new: str


class AppManifest:
    def __init__(self, path: Path, data: vdf.KV) -> None:
        self.path = path
        self.data = data
        state = data.get("AppState")
        if not isinstance(state, dict):
            raise ValueError(f"{path}: no AppState block")
        self.state: dict = state

    @classmethod
    def load(cls, path: Path | str) -> "AppManifest":
        p = Path(path)
        return cls(p, vdf.load(p))

    @staticmethod
    def find(libraries: Iterable[Library], appid: int) -> Path | None:
        for lib in libraries:
            p = lib.steamapps / f"appmanifest_{appid}.acf"
            if p.exists():
                return p
        return None

    # --- read ---
    @property
    def appid(self) -> int:
        return int(self.state.get("appid", 0))

    @property
    def buildid(self) -> int | None:
        try:
            return int(self.state.get("buildid"))
        except (TypeError, ValueError):
            return None

    @property
    def state_flags(self) -> int | None:
        try:
            return int(self.state.get("StateFlags"))
        except (TypeError, ValueError):
            return None

    @property
    def language(self) -> str:
        for block in ("UserConfig", "MountedConfig"):
            cfg = self.state.get(block)
            if isinstance(cfg, dict) and cfg.get("language"):
                return str(cfg["language"])
        return "english"

    @property
    def installed_depots(self) -> dict[int, str]:
        """depot id -> installed manifest gid."""
        out: dict[int, str] = {}
        block = self.state.get("InstalledDepots")
        if isinstance(block, dict):
            for k, v in block.items():
                if k.isdigit() and isinstance(v, dict) and v.get("manifest") is not None:
                    out[int(k)] = str(v["manifest"])
        return out

    def is_current(self, info: AppInfo) -> bool:
        """Would Steam consider this install up to date against ``info``?"""
        if info.public_buildid is None:
            return False
        if self.state_flags != STATE_FULLY_INSTALLED or self.buildid != info.public_buildid:
            return False
        for depot_id, gid in self.installed_depots.items():
            dep = info.depots.get(depot_id)
            if dep and dep.manifest_gid and dep.manifest_gid != gid:
                return False
        return True

    # --- pin ---
    def pin_to(self, info: AppInfo) -> list[PinChange]:
        """Rewrite the update-related fields so Steam sees the current public
        build as installed. Returns what changed (empty if already pinned)."""
        if info.public_buildid is None:
            raise ValueError("appinfo has no public build id for this app")
        if self.is_current(info):
            return []  # Steam already agrees; leave its bookkeeping alone
        changes: list[PinChange] = []

        def setf(key: str, value: str) -> None:
            old = self.state.get(key)
            if old != value:
                changes.append(PinChange(key, str(old) if old is not None else None, value))
                self.state[key] = value

        setf("StateFlags", str(STATE_FULLY_INSTALLED))
        setf("buildid", str(info.public_buildid))
        setf("TargetBuildID", "0")
        setf("UpdateResult", "0")
        for key in ("BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged"):
            if key in self.state:
                setf(key, "0")
        if "ScheduledAutoUpdate" in self.state:
            setf("ScheduledAutoUpdate", "0")

        depots = self.state.get("InstalledDepots")
        if isinstance(depots, dict):
            for k, v in depots.items():
                if not (k.isdigit() and isinstance(v, dict)):
                    continue
                dep = info.depots.get(int(k))
                if not dep or not dep.manifest_gid:
                    continue
                old = v.get("manifest")
                if old != dep.manifest_gid:
                    changes.append(PinChange(f"InstalledDepots/{k}/manifest", str(old), dep.manifest_gid))
                    v["manifest"] = dep.manifest_gid
                if dep.size is not None and str(v.get("size")) != str(dep.size):
                    changes.append(PinChange(f"InstalledDepots/{k}/size", str(v.get("size")), str(dep.size)))
                    v["size"] = str(dep.size)
        return changes

    # --- unpin ---
    def unpin(self, record: list[PinChange] | None = None) -> list[PinChange]:
        """Undo ``pin_to``. With ``record`` (what the pin changed) every field
        goes back to its old value; without it, mark the install as needing an
        update so Steam re-checks it. Returns what changed."""
        changes: list[PinChange] = []

        def setf(block: dict, key: str, value: str | None, label: str) -> None:
            old = block.get(key)
            if value is None:
                if key in block:
                    changes.append(PinChange(label, str(old), "(removed)"))
                    del block[key]
            elif old != value:
                changes.append(PinChange(label, str(old) if old is not None else None, value))
                block[key] = value

        if record:
            depots = self.state.get("InstalledDepots")
            for change in record:
                parts = change.field.split("/")
                if len(parts) == 3 and parts[0] == "InstalledDepots":
                    if isinstance(depots, dict) and isinstance(depots.get(parts[1]), dict):
                        setf(depots[parts[1]], parts[2], change.old, change.field)
                elif len(parts) == 1:
                    setf(self.state, change.field, change.old, change.field)
            return changes
        if self.state_flags == STATE_UPDATE_REQUIRED and self.buildid == 0:
            return []
        setf(self.state, "StateFlags", str(STATE_UPDATE_REQUIRED), "StateFlags")
        setf(self.state, "buildid", "0", "buildid")
        return changes

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".modsync-tmp")
        vdf.dump(self.data, tmp)
        tmp.replace(self.path)
