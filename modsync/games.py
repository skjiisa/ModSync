"""Supported games table. Adding a game is data, not new architecture."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    appid: int
    name: str
    mo2_game_name: str  # MO2's internal gameName / plugin name
    nexus_slug: str


SKYRIM_SE = Game(
    appid=489830,
    name="Skyrim Special Edition",
    mo2_game_name="Skyrim Special Edition",
    nexus_slug="skyrimspecialedition",
)

# Keyed by Steam AppID for quick lookup; extend over time.
GAMES: dict[int, Game] = {SKYRIM_SE.appid: SKYRIM_SE}
