"""Which game runtime is installed on this machine, and does it match the vault?

Skyrim SE's Steam runtime version (1.5.97, 1.6.1170, 1.7.104, ...) matters for a
synced MO2 instance: SKSE and every native DLL plugin in the vault are compiled
against one exact runtime. If the Deck is on 1.7.104 and the desktop is still on
1.6.1170, the same vault cannot work on both — and Steam updates the game
silently, so the two machines drift apart without anyone changing anything.

So ModSync records the runtime the vault was set up for in a small manifest
*inside* the synced instance (``modsync-vault.json``, whitelisted in
``.stignore``) and each machine compares its installed executable against it.
Detection reads the version resource of the game's main executable; the launcher
reports a useless 1.0.0.0, so we deliberately read ``SkyrimSE.exe`` itself.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from modsync import platforms
from modsync.games import SKYRIM_SE, Game
from modsync.mo2 import instance as mo2_instance
from modsync.steam import libraries as libs
from modsync.steam import pe

VAULT_META_NAME = "modsync-vault.json"


@dataclass(frozen=True)
class GameVersion:
    parts: tuple[int, int, int, int]

    @classmethod
    def parse(cls, text: str) -> "GameVersion":
        nums = [int(p) for p in text.strip().split(".")]
        if not 1 <= len(nums) <= 4:
            raise ValueError(f"not a version: {text!r}")
        nums += [0] * (4 - len(nums))
        return cls(tuple(nums))  # type: ignore[arg-type]

    def __str__(self) -> str:
        # Community convention: "1.6.1170", not "1.6.1170.0".
        a, b, c, d = self.parts
        return f"{a}.{b}.{c}" if d == 0 else f"{a}.{b}.{c}.{d}"


# --- what's installed here ----------------------------------------------------


def installed_version(game_dir: Path | str, game: Game = SKYRIM_SE) -> GameVersion | None:
    exe = Path(game_dir) / game.exe_name
    parts = pe.file_version(exe) if exe.is_file() else None
    return GameVersion(parts) if parts else None


def find_game_dir(instance_path: Path | str | None, game: Game = SKYRIM_SE) -> Path | None:
    """Prefer the game path MO2 itself is pointed at; fall back to Steam discovery."""
    if instance_path:
        info = mo2_instance.inspect(instance_path)
        if info.game_path_local and (info.game_path_local / game.exe_name).is_file():
            return info.game_path_local
    plat = platforms.current()
    app = libs.find_app(libs.all_libraries(plat.steam_roots()), game.appid)
    if app and app.install_path.is_dir():
        return app.install_path
    return None


# --- what the vault expects -----------------------------------------------------


@dataclass
class VaultMeta:
    """The shared, synced record of what the vault was built for."""

    appid: int
    runtime: str  # e.g. "1.6.1170"
    set_by: str = ""
    set_at: str = ""

    @staticmethod
    def path(instance_path: Path | str) -> Path:
        return Path(instance_path) / VAULT_META_NAME

    @classmethod
    def load(cls, instance_path: Path | str) -> "VaultMeta | None":
        p = cls.path(instance_path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            game = data["game"]
            return cls(
                appid=int(game["appid"]),
                runtime=str(game["runtime"]),
                set_by=str(game.get("set_by", "")),
                set_at=str(game.get("set_at", "")),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, instance_path: Path | str) -> Path:
        p = self.path(instance_path)
        payload = {
            "modsync": 1,
            "game": {
                "appid": self.appid,
                "runtime": self.runtime,
                "set_by": self.set_by,
                "set_at": self.set_at,
            },
        }
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return p

    @property
    def version(self) -> GameVersion | None:
        try:
            return GameVersion.parse(self.runtime)
        except ValueError:
            return None


def record_vault_version(
    instance_path: Path | str, version: GameVersion, game: Game = SKYRIM_SE
) -> VaultMeta:
    meta = VaultMeta(
        appid=game.appid,
        runtime=str(version),
        set_by=socket.gethostname() or "",
        set_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    meta.save(instance_path)
    return meta


# --- the comparison -------------------------------------------------------------


@dataclass
class VersionCheck:
    installed: GameVersion | None
    expected: GameVersion | None
    expected_by: str = ""
    game_dir: Path | None = None

    @property
    def mismatch(self) -> bool:
        return (
            self.installed is not None
            and self.expected is not None
            and self.installed != self.expected
        )

    @property
    def ok(self) -> bool:
        return (
            self.installed is not None
            and self.expected is not None
            and self.installed == self.expected
        )

    def summary(self, game: Game = SKYRIM_SE) -> str:
        """One human-readable line, shared by the doctor report and the dashboard."""
        if self.installed is None:
            return f"{game.name} runtime: could not be detected on this machine."
        if self.expected is None:
            return (
                f"{game.name} runtime here: {self.installed}. "
                "The vault does not record a version yet."
            )
        if self.mismatch:
            who = f" (set by {self.expected_by})" if self.expected_by else ""
            return (
                f"This machine runs {game.name} {self.installed}, but the vault was set "
                f"up for {self.expected}{who}. SKSE and native DLL mods in the vault will "
                "not load here until the versions match."
            )
        return f"{game.name} runtime: {self.installed} — matches the vault."


def check(instance_path: Path | str | None, game: Game = SKYRIM_SE) -> VersionCheck:
    game_dir = find_game_dir(instance_path, game)
    installed = installed_version(game_dir, game) if game_dir else None
    expected: GameVersion | None = None
    expected_by = ""
    if instance_path:
        meta = VaultMeta.load(instance_path)
        if meta and meta.appid == game.appid:
            expected = meta.version
            expected_by = meta.set_by
    return VersionCheck(installed, expected, expected_by, game_dir)
