"""The GOG games Heroic installed, read from its config folder.

Heroic keeps GOG installs in ``gog_store/installed.json`` (folder, GOG build,
whether that build is pinned) and each game's own settings in
``GamesConfig/<appName>.json`` (its Wine or Proton and its prefix). A setting
the game's file leaves out comes from Heroic's defaults in ``config.json``,
and a prefix missing there too is Heroic's shared one, the way Heroic 2.22's
``GameConfig`` resolves them. A prefix Heroic hasn't launched anything in yet
has no C: drive. The ``appName`` of a GOG game is its GOG product id.

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


@dataclass(frozen=True)
class GogGame:
    app_name: str  # the GOG product id
    install_path: Path
    version: str  # the GOG build's name, e.g. "2.5_RedModFix"
    build_id: str
    pinned: bool  # Heroic won't update past this build
    config_dir: Path  # the Heroic config folder it was found in
    prefix: Path | None = None  # the prefix Heroic runs it in (its compat data, for Proton)
    wine: WineVersion | None = None

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


def find_gog_game(app_name: str, config_dirs: Iterable[Path] | None = None) -> GogGame | None:
    """The GOG game with this product id, from the first Heroic that has it
    installed in a folder that still exists."""
    dirs = platforms.current().heroic_config_dirs() if config_dirs is None else config_dirs
    for config_dir in dirs:
        for game in gog_games(Path(config_dir)):
            if game.app_name == str(app_name) and game.install_path.is_dir():
                return game
    return None
