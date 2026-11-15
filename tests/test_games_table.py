"""The table of games ModSync runs Mod Organizer 2 for (modsync.games), and the
script that writes it from MO2-LINT's list."""

import importlib.util
import json
from pathlib import Path

from modsync import games
from modsync.mo2.installers.mo2lint import MO2LINT_VERSION

ROOT = Path(__file__).resolve().parent.parent


def test_the_table_is_the_pinned_mo2lints():
    """A new MO2-LINT pin means running scripts/build_game_table.py again."""
    assert json.loads(games.TABLE.read_text())["mo2lint"] == MO2LINT_VERSION


def test_every_game_can_be_found_in_a_store_and_named_to_mo2lint():
    assert len(games.MO2_GAMES) >= 20
    for key, game in games.MO2_GAMES.items():
        assert game.key == game.mo2lint_key == key
        assert game.stores, key
        assert game.name and game.mo2_game_name and game.nexus_slug, key
        for store in game.stores:
            assert game.exe_for(store), (key, store)


def test_a_store_id_belongs_to_one_game():
    """Variants inherit their parent's ids in MO2-LINT's list; a game a store
    has installed must be one game here."""
    seen: dict[tuple[str, str], str] = {}
    for key, game in games.MO2_GAMES.items():
        for store in game.stores:
            assert (store, game.store_id(store)) not in seen, (key, seen.get((store, game.store_id(store))))
            seen[(store, game.store_id(store))] = key
    # Skyrim Special Edition has a path of its own, and keeps its ids.
    assert "skyrim_se" not in games.MO2_GAMES and "skyrim_ae" not in games.MO2_GAMES
    assert games.MO2_GAMES["skyrim_vr"].stores == ("steam",)
    assert not any(g.appid == games.SKYRIM_SE.appid for g in games.MO2_GAMES.values())


def test_quirks_are_laid_over_the_table():
    cyberpunk = games.MO2_GAMES["cyberpunk2077"]
    assert cyberpunk is games.CYBERPUNK
    assert cyberpunk.dll_overrides == ("winmm", "version") and cyberpunk.play_argument == "-modded"
    assert cyberpunk.stores == ("steam", "gog", "epic")
    assert games.MO2_GAMES["falloutnv"].name == "Fallout: New Vegas"
    assert games.MO2_GAMES["falloutnv"].mo2_game_name == "New Vegas"  # what MO2 calls it stays
    assert games.MO2_GAMES["falloutlondon"].is_mo2_name("Fallout 4")
    assert set(games.QUIRKS) <= set(games.MO2_GAMES)


def test_a_program_can_differ_by_store():
    oblivion = games.MO2_GAMES["oblivion"]
    assert oblivion.exe_for("steam") == "OblivionLauncher.exe"
    assert oblivion.exe_for("epic") == "Oblivion GOTY English/OblivionLauncher.exe"


def test_games_are_found_by_steam_id():
    assert games.by_steam_id(489830) is games.SKYRIM_SE
    assert games.by_steam_id(1812390) is games.DAGGERFALL
    assert games.by_steam_id(1716740).key == "starfield"
    assert games.by_steam_id(1) is None


def _script():
    spec = importlib.util.spec_from_file_location("build_game_table", ROOT / "scripts" / "build_game_table.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_folds_a_parent_in_the_way_mo2lint_does():
    table = _script().convert({"games": {
        "base": {
            "display_name": "Base Game", "nexus_slug": "base",
            "launcher_ids": {"steam": 10, "gog": 20, "epic": "abc"},
            "executable": "Launcher.exe", "tricks": ["d3dx9"],
        },
        "variant": {
            "parent": "base", "display_name": "Variant",
            "launcher_ids": {"steam": None, "gog": 21},
            "executable": {"epic": "English/Launcher.exe"},
        },
    }})["games"]
    assert table["base"] == {"name": "Base Game", "nexus": "base", "steam": 10, "gog": "20", "epic": "abc",
                             "exe": {"steam": "Launcher.exe", "gog": "Launcher.exe", "epic": "Launcher.exe"},
                             "tricks": ["d3dx9"]}
    variant = table["variant"]
    assert variant["name"] == "Variant" and variant["nexus"] == "base" and variant["tricks"] == ["d3dx9"]
    assert (variant["steam"], variant["gog"], variant["epic"]) == (None, "21", "abc")
    assert variant["exe"] == {"gog": "Launcher.exe", "epic": "English/Launcher.exe"}


def test_the_checked_in_table_loads_from_another_place(tmp_path):
    copy = tmp_path / "games.json"
    copy.write_text(json.dumps({"mo2lint": "0", "games": {
        "one": {"name": "One", "nexus": "one", "steam": 5, "gog": "6", "epic": None,
                "exe": {"steam": "one.exe", "gog": "bin/one.exe"}, "tricks": ["xact"]},
        "one_again": {"name": "One Again", "nexus": "one", "steam": 5, "gog": None, "epic": None,
                      "exe": {"steam": "one.exe"}, "tricks": []},
    }}))
    table = games._load_table(copy)
    assert list(table) == ["one"]  # the second has no id left of its own
    assert table["one"].exe_for("gog") == "bin/one.exe" and table["one"].tricks == ("xact",)
    assert games._load_table(tmp_path / "missing.json") == {}
