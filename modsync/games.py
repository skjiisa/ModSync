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


SKYRIM_SE = Game(
    appid=489830,
    name="Skyrim Special Edition",
    mo2_game_name="Skyrim Special Edition",
    nexus_slug="skyrimspecialedition",
    mo2lint_key="skyrim_se",
)

# Keyed by Steam AppID for quick lookup; extend over time.
GAMES: dict[int, Game] = {SKYRIM_SE.appid: SKYRIM_SE}
