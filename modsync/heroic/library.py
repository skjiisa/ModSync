"""The GOG games Heroic installed, read from its config folder.

Heroic keeps GOG installs in ``gog_store/installed.json`` (folder, GOG build,
whether that build is pinned) and each game's own settings in
``GamesConfig/<appName>.json`` (its Wine or Proton and its prefix). The second
file appears once the game's settings are first saved, which launching it does,
so a game Heroic has never started has no prefix yet. The ``appName`` of a GOG
game is its GOG product id.

Heroic runs natively (``~/.config/heroic``) or as the Flatpak
``com.heroicgameslauncher.hgl``; both are read, native first.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from modsync import platforms

log = logging.getLogger(__name__)


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
    prefix: Path | None = None  # None until Heroic has launched it once
    wine: WineVersion | None = None

    @property
    def flatpak(self) -> bool:
        return "com.heroicgameslauncher.hgl" in self.config_dir.parts

    @property
    def drive_c(self) -> Path | None:
        """The prefix's C: drive. Heroic's Proton prefixes link ``pfx`` to
        themselves, so either layout works."""
        if self.prefix is None:
            return None
        drive = self.prefix / "drive_c"
        return drive if drive.is_dir() else None


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _wine_version(raw: object) -> WineVersion | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("bin"), str) or not raw["bin"]:
        return None
    return WineVersion(Path(raw["bin"]), str(raw.get("name", "")), str(raw.get("type", "")))


def _game_settings(config_dir: Path, app_name: str) -> tuple[Path | None, WineVersion | None]:
    """The prefix and Wine from the game's own settings, falling back to
    Heroic's defaults for the Wine (never for the prefix, which differs per game)."""
    data = _read_json(config_dir / "GamesConfig" / f"{app_name}.json")
    settings = data.get(app_name) if isinstance(data, dict) else None
    prefix = wine = None
    if isinstance(settings, dict):
        raw_prefix = settings.get("winePrefix")
        if isinstance(raw_prefix, str) and raw_prefix:
            prefix = Path(raw_prefix).expanduser()
        wine = _wine_version(settings.get("wineVersion"))
    if wine is None:
        defaults = _read_json(config_dir / "config.json")
        if isinstance(defaults, dict) and isinstance(defaults.get("defaultSettings"), dict):
            wine = _wine_version(defaults["defaultSettings"].get("wineVersion"))
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
        app_name, install_path = entry.get("appName"), entry.get("install_path")
        if not isinstance(app_name, str) or not app_name or not isinstance(install_path, str) or not install_path:
            continue
        prefix, wine = _game_settings(config_dir, app_name)
        games.append(GogGame(
            app_name=app_name,
            install_path=Path(install_path).expanduser(),
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
