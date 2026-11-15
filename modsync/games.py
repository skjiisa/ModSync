"""The games ModSync knows. Adding a game is data, not new architecture.

Three games have a path of their own: Skyrim Special Edition on Steam (the MO2
vault, the game version, SKSE), Daggerfall (Daggerfall Unity in place of the
game) and Cyberpunk 2077 (a :class:`modsync.mo2games.ManagedGame` with extras).
Every other game is a row of :data:`MO2_GAMES`, read from ``mo2/games.json``,
which ``scripts/build_game_table.py`` writes from MO2-LINT's own list: what
MO2-LINT can install Mod Organizer 2 for, ModSync can find in Steam or Heroic,
play through MO2 and sync the mods of (see :mod:`modsync.mo2games`).

What a game needs beyond that goes in :data:`QUIRKS` when it is a fact (DLLs
Wine must load from the game's folder, which of MO2's programs plays the game),
and in a subclass of ``ManagedGame`` when it is something to do (a plugin to
add after installing MO2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

STORES = ("steam", "gog", "epic")
STORE_NAMES = {"steam": "Steam", "gog": "GOG", "epic": "Epic"}


@dataclass(frozen=True)
class Game:
    appid: int  # Steam's id; 0 when Steam doesn't have the game
    name: str
    mo2_game_name: str  # MO2's internal gameName / plugin name
    nexus_slug: str
    mo2lint_key: str  # MO2-LINT's game key (see its configs/game_info.yml)
    exe_name: str  # main game executable — its version resource is the runtime version
    gog_id: str = ""  # GOG product id, which Heroic calls the game's appName
    epic_id: str = ""  # Epic's app name, which Heroic calls the game's appName too
    key: str = ""  # ModSync's name for it: 'modsync games <key>', its settings file, its synced folder
    # The program each store starts, relative to the game's folder, where it
    # isn't exe_name: (store, path) pairs. Often a launcher.
    store_exes: tuple[tuple[str, str], ...] = ()
    tricks: tuple[str, ...] = ()  # the winetricks verbs MO2-LINT applies to its prefix
    # --- what a game needs beyond the table (QUIRKS) ---
    # Other names MO2 may give the game in ModOrganizer.ini.
    mo2_names: tuple[str, ...] = ()
    # DLLs Wine must load from the game's folder before its own: the proxy DLLs
    # mod loaders arrive as. Set on every launch as WINEDLLOVERRIDES "…=n,b".
    dll_overrides: tuple[str, ...] = ()
    # Which of MO2's programs Play runs: only ones with these file names, the
    # first name first. Empty: the first program MO2 lists from the game's folder.
    play_exes: tuple[str, ...] = ()
    play_argument: str = ""  # among those, the one with this argument wins
    direct_arguments: str = ""  # the game's arguments while MO2 has listed no program yet

    def store_id(self, store: str) -> str:
        """The game's id in a store, "" when the store doesn't have it."""
        return {"steam": str(self.appid) if self.appid else "", "gog": self.gog_id, "epic": self.epic_id}[store]

    @property
    def stores(self) -> tuple[str, ...]:
        return tuple(s for s in STORES if self.store_id(s))

    def exe_for(self, store: str) -> str:
        return dict(self.store_exes).get(store) or self.exe_name

    def is_mo2_name(self, name: str) -> bool:
        return name in (self.mo2_game_name, *self.mo2_names)


SKYRIM_SE = Game(
    appid=489830,
    name="Skyrim Special Edition",
    mo2_game_name="Skyrim Special Edition",
    nexus_slug="skyrimspecialedition",
    mo2lint_key="skyrim_se",
    exe_name="SkyrimSE.exe",  # not the launcher, which reports 1.0.0.0
    key="skyrim_se",
)

# Keyed by Steam AppID for quick lookup: the Steam games with a path of their own.
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

# --- the table ------------------------------------------------------------------

TABLE = Path(__file__).parent / "mo2" / "games.json"

# In MO2-LINT's list, but not offered as a generic game: ModSync has its own
# path for it. It still claims its store ids, so a variant that inherited them
# (Skyrim VR "on GOG") doesn't.
OWN_PATH = frozenset({"skyrim_se"})

# What a game needs beyond MO2-LINT's facts, by its key. ``name`` here is how
# ModSync shows it, where MO2's name for it is short or shared by two editions.
QUIRKS: dict[str, dict] = {
    "cyberpunk2077": {
        # Cyber Engine Tweaks and RED4ext load through these, next to the game.
        "dll_overrides": ("winmm", "version"),
        # MO2's Cyberpunk plugin lists the game twice: with "-modded" it deploys REDmod first.
        "play_exes": ("Cyberpunk2077.exe",),
        "play_argument": "-modded",
        "direct_arguments": "--launcher-skip -modded",
    },
    "falloutnv": {"name": "Fallout: New Vegas"},
    "falloutnv_ru": {"name": "Fallout: New Vegas PCR"},  # Steam's Polish, Czech and Russian release
    "fallout3_goty": {"name": "Fallout 3: Game of the Year Edition"},
    "falloutlondon": {"name": "Fallout: London", "mo2_names": ("Fallout 4",)},
    "skyrim": {"name": "Skyrim Legendary Edition"},
    "witcher3_goty": {"name": "The Witcher 3: Wild Hunt (Game of the Year Edition)"},
}


def _load_table(path: Path = TABLE) -> dict[str, Game]:
    """MO2-LINT's games as ModSync's. A store id belongs to the first game
    that lists it: variants inherit their parent's ids, and two games can't
    both be the one a store has installed."""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))["games"]
    except (OSError, ValueError, KeyError):
        return {}
    claimed: set[tuple[str, str]] = set()
    games: dict[str, Game] = {}
    for key, entry in entries.items():
        ids = {store: str(entry.get(store) or "") for store in STORES}
        for store in STORES:
            if ids[store] and (store, ids[store]) in claimed:
                ids[store] = ""
        claimed |= {(store, value) for store, value in ids.items() if value}
        if key in OWN_PATH or not any(ids.values()):
            continue
        programs = {store: exe for store, exe in (entry.get("exe") or {}).items() if ids.get(store)}
        exe = next((programs[s] for s in STORES if programs.get(s)), "")
        game = Game(
            appid=int(ids["steam"] or 0),
            name=entry["name"],
            mo2_game_name=entry["name"],  # MO2-LINT writes its display name as MO2's gameName
            nexus_slug=entry.get("nexus") or "",
            mo2lint_key=key,
            exe_name=exe,
            gog_id=ids["gog"],
            epic_id=ids["epic"],
            key=key,
            store_exes=tuple((s, p) for s, p in programs.items() if p != exe),
            tricks=tuple(entry.get("tricks") or ()),
        )
        games[key] = replace(game, **QUIRKS.get(key, {}))
    return games


# Every game ModSync runs Mod Organizer 2 for the same way, by key.
MO2_GAMES: dict[str, Game] = _load_table()

CYBERPUNK = MO2_GAMES["cyberpunk2077"]


def by_steam_id(appid: int) -> Game | None:
    """The game with this Steam id: one with a path of its own, else one from the table."""
    if appid in GAMES:
        return GAMES[appid]
    if appid == DAGGERFALL.appid:
        return DAGGERFALL
    return next((g for g in MO2_GAMES.values() if g.appid == appid), None)
