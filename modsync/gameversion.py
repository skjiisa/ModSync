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

The installed SKSE is a second, independent clue. Its runtime DLL is named after
the exact game version it was built for (``skse64_1_6_1170.dll``), so an existing
MO2 setup carries a record of the runtime it was made for even when nobody wrote
it down. That is what lets ModSync suggest the right downgrade target for an
instance that is being imported after Steam already updated the game.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass, field
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


# --- what SKSE was built for ----------------------------------------------------

# skse64_1_6_1170.dll, skse64_1_5_97.dll, skse64_1_6_659_gog.dll, ...
_SKSE_DLL = re.compile(r"^skse64_(\d+)_(\d+)_(\d+)(?:_[a-z]+)?\.dll$", re.IGNORECASE)


@dataclass(frozen=True)
class SkseFile:
    path: Path
    runtime: GameVersion
    where: str  # "game folder" or "mod <name>"


@dataclass
class SkseCheck:
    """Every SKSE runtime DLL found for this setup, and what they agree on."""

    files: list[SkseFile] = field(default_factory=list)

    @property
    def runtimes(self) -> list[GameVersion]:
        return sorted({f.runtime for f in self.files}, key=lambda v: v.parts)

    @property
    def runtime(self) -> GameVersion | None:
        """The one runtime the installed SKSE is built for; None when there is
        no SKSE or when DLLs for several runtimes are lying around."""
        rts = self.runtimes
        return rts[0] if len(rts) == 1 else None

    @property
    def ambiguous(self) -> bool:
        return len(self.runtimes) > 1

    def describe(self) -> str:
        """Short provenance for one runtime: "skse64_1_6_1170.dll in the game folder"."""
        if not self.files:
            return ""
        f = self.files[0]
        return f"{f.path.name} in the {f.where}"


def _skse_dlls_in(folder: Path, where: str) -> list[SkseFile]:
    out: list[SkseFile] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return out
    for entry in entries:
        m = _SKSE_DLL.match(entry.name)
        if not m or not entry.is_file():
            continue
        runtime = GameVersion((int(m[1]), int(m[2]), int(m[3]), 0))
        out.append(SkseFile(entry, runtime, where))
    return out


def scan_skse(game_dir: Path | str | None, instance_path: Path | str | None = None) -> SkseCheck:
    """Look for SKSE runtime DLLs next to the game and inside the MO2 mods folder.

    SKSE normally lives in the game folder. Some people keep it in a mod instead,
    either at the mod's top level or under ``Root/`` (the Root Builder plugin
    convention), so those are scanned too. Only the mods folder's first level is
    read; a mod list can hold thousands of files and this must stay cheap.
    """
    files: list[SkseFile] = []
    if game_dir:
        files += _skse_dlls_in(Path(game_dir), "game folder")
    if instance_path:
        mods_dir = _mods_dir(Path(instance_path))
        if mods_dir is not None and mods_dir.is_dir():
            try:
                mods = sorted(d for d in mods_dir.iterdir() if d.is_dir())
            except OSError:
                mods = []
            for mod in mods:
                files += _skse_dlls_in(mod, f"mod \u201c{mod.name}\u201d")
                files += _skse_dlls_in(mod / "Root", f"mod \u201c{mod.name}\u201d")
    return SkseCheck(files)


def _mods_dir(instance_path: Path) -> Path | None:
    try:
        info = mo2_instance.inspect(instance_path)
    except OSError:
        return None
    content = info.content_dirs.get("mods")
    return content.path if content and content.path else instance_path / "mods"


def choose_vault_version(installed: GameVersion | None, skse: SkseCheck) -> tuple[GameVersion | None, str]:
    """Which runtime a *new* vault should record, and where that answer came from.

    An existing mod list was built against the SKSE that is installed with it,
    not against whatever Steam happens to have patched the game to since. So
    when SKSE unambiguously names a runtime, that wins over the installed
    executable. Returns (version, "skse" | "game" | "")."""
    if skse.runtime is not None:
        return skse.runtime, "skse"
    if installed is not None:
        return installed, "game"
    return None, ""


# --- what the vault expects -----------------------------------------------------


@dataclass
class VaultMeta:
    """The shared, synced record of what the vault was built for."""

    appid: int
    runtime: str  # e.g. "1.6.1170"
    set_by: str = ""
    set_at: str = ""
    set_from: str = ""  # "game" (the installed exe) | "skse" (the installed SKSE) | ""

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
                set_from=str(game.get("set_from", "")),
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
        if self.set_from:
            payload["game"]["set_from"] = self.set_from
        p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return p

    @property
    def version(self) -> GameVersion | None:
        try:
            return GameVersion.parse(self.runtime)
        except ValueError:
            return None


def record_vault_version(
    instance_path: Path | str, version: GameVersion, game: Game = SKYRIM_SE, *, source: str = "game"
) -> VaultMeta:
    meta = VaultMeta(
        appid=game.appid,
        runtime=str(version),
        set_by=socket.gethostname() or "",
        set_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        set_from=source,
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
    expected_from: str = ""
    skse: SkseCheck = field(default_factory=SkseCheck)

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
                "No version is recorded for this setup yet."
            )
        if self.mismatch:
            who = f" (set by {self.expected_by})" if self.expected_by else ""
            if self.expected_by and self.expected_from == "skse":
                who = f" (set by {self.expected_by} from its installed SKSE)"
            return (
                f"This machine runs {game.name} {self.installed}, but this setup was "
                f"built for {self.expected}{who}. SKSE and native DLL mods will not load "
                "here until the versions match."
            )
        return f"{game.name} runtime: {self.installed} — matches what this setup was built for."

    @property
    def skse_suggests(self) -> GameVersion | None:
        """The runtime the installed SKSE points at, when the vault has no say
        and SKSE disagrees with the installed game. This is the "you imported
        an old setup and Steam updated the game since" signal."""
        rt = self.skse.runtime
        if rt is None or self.expected is not None or self.installed is None or rt == self.installed:
            return None
        return rt

    def skse_note(self) -> str:
        """An extra line about the installed SKSE, or "" when it adds nothing.
        Shown under ``summary()`` by the dashboard, doctor and CLI."""
        if self.installed is None or not self.skse.files:
            return ""
        if self.skse.ambiguous:
            versions = ", ".join(str(v) for v in self.skse.runtimes)
            return (
                f"SKSE DLLs for several game versions are installed ({versions}), "
                "so SKSE cannot tell which version this setup was built for."
            )
        rt = self.skse.runtime
        assert rt is not None
        src = self.skse.describe()
        if self.expected is None:
            if rt == self.installed:
                return f"The installed SKSE ({src}) is built for {rt}, matching the game."
            return (
                f"The installed SKSE ({src}) is built for {rt}, so this setup was most "
                f"likely made for {rt}, not for the {self.installed} that is installed now."
            )
        if rt == self.expected and self.mismatch:
            return f"The installed SKSE ({src}) is built for {rt}: it will work again once the game is {rt}."
        if rt != self.expected:
            return (
                f"The installed SKSE ({src}) is built for {rt}, but this setup was built for "
                f"{self.expected}. SKSE will need to be reinstalled for that version."
            )
        return ""


def check(instance_path: Path | str | None, game: Game = SKYRIM_SE) -> VersionCheck:
    game_dir = find_game_dir(instance_path, game)
    installed = installed_version(game_dir, game) if game_dir else None
    expected: GameVersion | None = None
    expected_by = ""
    expected_from = ""
    if instance_path:
        meta = VaultMeta.load(instance_path)
        if meta and meta.appid == game.appid:
            expected = meta.version
            expected_by = meta.set_by
            expected_from = meta.set_from
    skse = scan_skse(game_dir, instance_path) if game_dir or instance_path else SkseCheck()
    return VersionCheck(installed, expected, expected_by, game_dir, expected_from, skse)
