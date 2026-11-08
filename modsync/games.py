"""Supported games table. Adding a game is data, not new architecture."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    appid: int
    name: str
    mo2_game_name: str  # MO2's internal gameName / plugin name
    nexus_slug: str
    mo2lint_key: str  # MO2-LINT's game key (see its configs/game_info.yml)
    exe_name: str  # main game executable — its version resource is the runtime version
    gog_id: str = ""  # GOG product id, which Heroic calls the game's appName


SKYRIM_SE = Game(
    appid=489830,
    name="Skyrim Special Edition",
    mo2_game_name="Skyrim Special Edition",
    nexus_slug="skyrimspecialedition",
    mo2lint_key="skyrim_se",
    exe_name="SkyrimSE.exe",  # not the launcher, which reports 1.0.0.0
)

# Keyed by Steam AppID for quick lookup; extend over time.
GAMES: dict[int, Game] = {SKYRIM_SE.appid: SKYRIM_SE}

# Not an MO2 game, so not in GAMES: Steam's Play button on it can start
# Daggerfall Unity instead (modsync.dfu, modsync.launchhook).
DAGGERFALL = Game(
    appid=1812390,
    name="The Elder Scrolls II: Daggerfall",
    mo2_game_name="",
    nexus_slug="",
    mo2lint_key="",
    exe_name="",
)

# Not in GAMES: ModSync finds it through Heroic (GOG) rather than Steam, and the
# Steam-side features don't handle it yet (modsync.heroic).
CYBERPUNK = Game(
    appid=1091500,
    name="Cyberpunk 2077",
    mo2_game_name="Cyberpunk 2077",
    nexus_slug="cyberpunk2077",
    mo2lint_key="cyberpunk2077",
    exe_name="bin/x64/Cyberpunk2077.exe",
    gog_id="1423049311",
)
