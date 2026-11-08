"""Reading the GOG games Heroic installed (modsync.heroic.library)."""

import json
from pathlib import Path

from modsync.heroic import library
from modsync.platforms.linux import LinuxPlatform

CYBERPUNK_GOG = "1423049311"
PROTON = "/usr/share/steam/compatibilitytools.d/proton-cachyos-native/proton"


def make_heroic(root: Path, *, installed: list[dict], games_config: dict[str, dict] | None = None,
                default_wine: dict | None = None) -> Path:
    """A Heroic config folder laid out the way Heroic 2.22 writes it."""
    (root / "gog_store").mkdir(parents=True)
    (root / "gog_store" / "installed.json").write_text(json.dumps({"installed": installed}))
    (root / "GamesConfig").mkdir()
    for app_name, settings in (games_config or {}).items():
        (root / "GamesConfig" / f"{app_name}.json").write_text(
            json.dumps({app_name: settings, "version": "v0", "explicit": True}))
    defaults = {"defaultSettings": {"wineVersion": default_wine}} if default_wine else {"defaultSettings": {}}
    (root / "config.json").write_text(json.dumps(defaults))
    return root


def cyberpunk_entry(install_path: Path, **extra) -> dict:
    return {
        "platform": "windows",
        "install_path": str(install_path),
        "is_dlc": False,
        "version": "2.5_RedModFix",
        "appName": CYBERPUNK_GOG,
        "buildId": "60152385347545148",
        "pinnedVersion": True,
        **extra,
    }


def test_reads_install_build_prefix_and_proton(tmp_path):
    game_dir = tmp_path / "Games/Heroic/Cyberpunk 2077"
    game_dir.mkdir(parents=True)
    prefix = tmp_path / "Games/Heroic/Prefixes/Cyberpunk 2077"
    heroic = make_heroic(tmp_path / "heroic", installed=[cyberpunk_entry(game_dir)], games_config={
        CYBERPUNK_GOG: {"winePrefix": str(prefix),
                        "wineVersion": {"bin": PROTON, "name": "proton-cachyos-native", "type": "proton"}},
    })

    game = library.find_gog_game(CYBERPUNK_GOG, [heroic])

    assert game is not None
    assert game.install_path == game_dir
    assert game.version == "2.5_RedModFix" and game.build_id == "60152385347545148" and game.pinned
    assert game.prefix == prefix
    assert game.wine.is_proton and game.wine.proton_dir == Path(PROTON).parent
    assert not game.flatpak


def test_never_launched_game_has_no_prefix_and_uses_default_wine(tmp_path):
    game_dir = tmp_path / "Cyberpunk 2077"
    game_dir.mkdir()
    heroic = make_heroic(tmp_path / "heroic", installed=[cyberpunk_entry(game_dir)],
                         default_wine={"bin": PROTON, "name": "proton-cachyos", "type": "proton"})

    game = library.find_gog_game(CYBERPUNK_GOG, [heroic])

    assert game.prefix is None and game.drive_c is None
    assert game.wine.name == "proton-cachyos"


def test_skips_dlc_and_installs_whose_folder_is_gone(tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    heroic_a = make_heroic(tmp_path / "a", installed=[
        cyberpunk_entry(tmp_path / "gone"),
        {**cyberpunk_entry(present), "appName": "1256837418", "is_dlc": True},
    ])
    heroic_b = make_heroic(tmp_path / "b", installed=[cyberpunk_entry(present)])

    assert [g.app_name for g in library.gog_games(heroic_a)] == [CYBERPUNK_GOG]
    # The first Heroic lists it in a folder that's gone, so the second one's wins.
    assert library.find_gog_game(CYBERPUNK_GOG, [heroic_a, heroic_b]).install_path == present
    assert library.find_gog_game("1256837418", [heroic_a]) is None


def test_unreadable_or_missing_files_find_nothing(tmp_path):
    broken = tmp_path / "broken"
    (broken / "gog_store").mkdir(parents=True)
    (broken / "gog_store" / "installed.json").write_text("{not json")

    assert library.gog_games(broken) == []
    assert library.gog_games(tmp_path / "missing") == []


def test_drive_c_and_flatpak(tmp_path):
    config_dir = tmp_path / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    game = library.GogGame(CYBERPUNK_GOG, tmp_path, "", "", False, config_dir, prefix=prefix)

    assert game.flatpak
    assert game.drive_c == prefix / "drive_c"


def test_platform_finds_native_and_flatpak_heroic(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert LinuxPlatform().heroic_config_dirs() == []
    (tmp_path / ".config/heroic").mkdir(parents=True)
    (tmp_path / ".var/app/com.heroicgameslauncher.hgl/config/heroic").mkdir(parents=True)

    assert LinuxPlatform().heroic_config_dirs() == [
        tmp_path / ".config/heroic",
        tmp_path / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
    ]


def test_doctor_reports_cyberpunk_from_heroic_without_steam(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import patch

    from modsync import report

    game_dir = tmp_path / "Cyberpunk 2077"
    game_dir.mkdir()
    heroic = make_heroic(tmp_path / "heroic", installed=[cyberpunk_entry(game_dir)], games_config={
        CYBERPUNK_GOG: {"winePrefix": str(tmp_path / "prefix"),
                        "wineVersion": {"bin": PROTON, "name": "proton-cachyos-native", "type": "proton"}},
    })
    plat = SimpleNamespace(name="linux", steam_roots=lambda: [], heroic_config_dirs=lambda: [heroic])

    with patch.object(report.platforms, "current", return_value=plat):
        result = report.build()

    assert not result.steam_found
    assert "Cyberpunk 2077 (GOG 1423049311, Heroic):" in result.text
    assert f"✓ installed: {game_dir}" in result.text
    assert "GOG build: 2.5_RedModFix (pinned in Heroic)" in result.text
    assert "runs with: proton-cachyos-native (proton)" in result.text
