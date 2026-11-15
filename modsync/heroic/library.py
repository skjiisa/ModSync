"""The GOG and Epic games Heroic installed, read from its config folder.

Heroic keeps GOG installs in ``gog_store/installed.json`` (folder, GOG build,
whether that build is pinned), Epic installs in Legendary's
``legendaryConfig/legendary/installed.json`` (keyed by Epic's app name), and
each game's own settings in ``GamesConfig/<appName>.json`` (its Wine or Proton
and its prefix). A setting the game's file leaves out comes from Heroic's
defaults in ``config.json``, and a prefix missing there too is Heroic's shared
one, the way Heroic 2.22's ``GameConfig`` resolves them. A prefix Heroic hasn't
launched anything in yet has no C: drive. The ``appName`` of a GOG game is its
GOG product id; an Epic game's is Epic's app name.

Heroic runs natively (``~/.config/heroic``) or as the Flatpak
``com.heroicgameslauncher.hgl``; both are read, native first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from modsync import platforms

# Heroic's own prefix when neither the game nor its defaults name one.
SHARED_PREFIX = Path("Games/Heroic/Prefixes/shared")


@dataclass(frozen=True)
class WineVersion:
    """The Wine or Proton Heroic runs a game with."""

    bin: Path  # ``…/proton`` for Proton, the ``wine`` binary otherwise
    name: str
    type: str  # "proton", "wine", "toolkit" or "crossover"

    @property
    def is_proton(self) -> bool:
        return self.type == "proton"

    @property
    def proton_dir(self) -> Path | None:
        """The Proton folder (what umu-launcher's PROTONPATH names)."""
        return self.bin.parent if self.is_proton else None


# Heroic's name for each store's backend ("runner"), by ModSync's name for the store.
RUNNERS = {"gog": "gog", "epic": "legendary"}


@dataclass(frozen=True)
class HeroicGame:
    app_name: str  # the GOG product id, or Epic's app name
    install_path: Path
    version: str  # the build's name, e.g. "2.5_RedModFix"
    build_id: str
    pinned: bool  # Heroic won't update past this build
    config_dir: Path  # the Heroic config folder it was found in
    prefix: Path | None = None  # the prefix Heroic runs it in (its compat data, for Proton)
    wine: WineVersion | None = None
    runner: str = "gog"  # Heroic's backend for the store: "gog", or "legendary" for Epic

    @property
    def store(self) -> str:
        """ModSync's name for the store: "gog" or "epic"."""
        return next((store for store, runner in RUNNERS.items() if runner == self.runner), self.runner)

    @property
    def flatpak(self) -> bool:
        return "com.heroicgameslauncher.hgl" in self.config_dir.parts

    @property
    def drive_c(self) -> Path | None:
        """The prefix's C: drive, or None before anything has run in it. Under
        Proton the Wine prefix is ``pfx`` inside the compat data folder (Heroic's
        often links it to the folder itself); plain Wine uses the folder."""
        if self.prefix is None:
            return None
        layouts = ("pfx/drive_c", "drive_c") if self.wine is not None and self.wine.is_proton else ("drive_c", "pfx/drive_c")
        return next((self.prefix / d for d in layouts if (self.prefix / d).is_dir()), None)


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _heroic_path(raw: object) -> Path | None:
    """A path from Heroic's settings. Heroic swaps a ``~`` for the home folder
    itself and knows no ``~user``, so neither does this (``expanduser`` would
    raise on one)."""
    if not isinstance(raw, str) or not raw:
        return None
    return Path(raw.replace("~", str(Path.home()), 1))


def _wine_version(raw: object) -> WineVersion | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("bin"), str) or not raw["bin"]:
        return None
    return WineVersion(Path(raw["bin"]), str(raw.get("name", "")), str(raw.get("type", "")))


def _settings(data: object, key: str) -> dict:
    found = data.get(key) if isinstance(data, dict) else None
    return found if isinstance(found, dict) else {}


def _game_settings(config_dir: Path, app_name: str) -> tuple[Path, WineVersion | None]:
    """The prefix and Wine Heroic runs the game with: its own settings, then
    Heroic's defaults, then (for the prefix) Heroic's shared prefix."""
    game = _settings(_read_json(config_dir / "GamesConfig" / f"{app_name}.json"), app_name)
    defaults = _settings(_read_json(config_dir / "config.json"), "defaultSettings")
    prefix = (_heroic_path(game.get("winePrefix")) or _heroic_path(defaults.get("winePrefix"))
              or Path.home() / SHARED_PREFIX)
    wine = _wine_version(game.get("wineVersion")) or _wine_version(defaults.get("wineVersion"))
    return prefix, wine


GogGame = HeroicGame  # what it was called while GOG was the only store read


def gog_games(config_dir: Path) -> list[GogGame]:
    """Every GOG game (not DLC) Heroic installed from this config folder."""
    data = _read_json(config_dir / "gog_store" / "installed.json")
    entries = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    games: list[GogGame] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("is_dlc"):
            continue
        app_name, install_path = entry.get("appName"), _heroic_path(entry.get("install_path"))
        if not isinstance(app_name, str) or not app_name or install_path is None:
            continue
        prefix, wine = _game_settings(config_dir, app_name)
        games.append(GogGame(
            app_name=app_name,
            install_path=install_path,
            version=str(entry.get("version") or ""),
            build_id=str(entry.get("buildId") or ""),
            pinned=bool(entry.get("pinnedVersion")),
            config_dir=config_dir,
            prefix=prefix,
            wine=wine,
        ))
    return games


def epic_games(config_dir: Path) -> list[HeroicGame]:
    """Every Epic game (not DLC) Heroic installed from this config folder.
    Legendary has no pinned builds; Heroic just doesn't update a game by itself."""
    data = _read_json(config_dir / "legendaryConfig" / "legendary" / "installed.json")
    if not isinstance(data, dict):
        return []
    games: list[HeroicGame] = []
    for app_name, entry in data.items():
        if not isinstance(entry, dict) or entry.get("is_dlc") or not isinstance(app_name, str) or not app_name:
            continue
        if str(entry.get("platform") or "Windows").lower() not in ("windows", "win32"):
            continue  # a macOS build: nothing Mod Organizer 2 can run
        install_path = _heroic_path(entry.get("install_path"))
        if install_path is None:
            continue
        prefix, wine = _game_settings(config_dir, app_name)
        games.append(HeroicGame(
            app_name=app_name,
            install_path=install_path,
            version=str(entry.get("version") or ""),
            build_id="",
            pinned=False,
            config_dir=config_dir,
            prefix=prefix,
            wine=wine,
            runner=RUNNERS["epic"],
        ))
    return games


def installed(config_dirs: Iterable[Path] | None = None) -> list[HeroicGame]:
    """Every GOG and Epic game Heroic has installed in a folder that still
    exists, each Heroic (native, then Flatpak) in turn."""
    dirs = platforms.current().heroic_config_dirs() if config_dirs is None else config_dirs
    return [game for config_dir in dirs
            for game in (*gog_games(Path(config_dir)), *epic_games(Path(config_dir)))
            if game.install_path.is_dir()]


def find_game(store: str, app_name: str, config_dirs: Iterable[Path] | None = None) -> HeroicGame | None:
    """The game with this id in ``store`` ("gog" or "epic"), from the first
    Heroic that has it installed in a folder that still exists."""
    dirs = platforms.current().heroic_config_dirs() if config_dirs is None else config_dirs
    read = {"gog": gog_games, "epic": epic_games}.get(store)
    if read is None or not app_name:
        return None
    for config_dir in dirs:
        for game in read(Path(config_dir)):
            if game.app_name == str(app_name) and game.install_path.is_dir():
                return game
    return None


def find_gog_game(app_name: str, config_dirs: Iterable[Path] | None = None) -> GogGame | None:
    """The GOG game with this product id, from the first Heroic that has it
    installed in a folder that still exists."""
    return find_game("gog", app_name, config_dirs)
