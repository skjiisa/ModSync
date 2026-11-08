"""Cyberpunk 2077 through Heroic, modded with MO2 (modsync.cyberpunk)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from modsync import cyberpunk
from modsync.heroic import library

PROTON = Path("/opt/proton-cachyos-native/proton")


def make_game(tmp_path: Path, *, wine_type: str = "proton", prefix_ready: bool = True) -> library.GogGame:
    install = tmp_path / "Cyberpunk 2077"
    (install / "bin/x64").mkdir(parents=True)
    (install / "bin/x64/Cyberpunk2077.exe").write_bytes(b"MZ")
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    if prefix_ready:
        (prefix / "drive_c").mkdir()
    wine = library.WineVersion(PROTON if wine_type == "proton" else Path("/usr/bin/wine"), "x", wine_type)
    return library.GogGame("1423049311", install, "2.5", "1", True, tmp_path / "heroic", prefix=prefix, wine=wine)


def make_instance(tmp_path: Path, ini: str = "") -> Path:
    instance = tmp_path / "ModOrganizer2-Cyberpunk2077"
    instance.mkdir()
    (instance / "ModOrganizer.exe").write_bytes(b"MZ")
    (instance / "ModOrganizer.ini").write_text(ini or "[General]\ngameName=Cyberpunk 2077\n"
                                                      "selected_profile=@ByteArray(Default)\n")
    return instance


PLUGIN_EXECUTABLES = r"""[General]
gameName=Cyberpunk 2077
selected_profile=@ByteArray(Default)

[customExecutables]
size=2
1\arguments="--launcher-skip "
1\binary=Z:/games/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe
1\title=Cyberpunk 2077 - skip REDmod deploy
2\arguments=--launcher-skip -modded
2\binary=Z:/games/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe
2\title=Cyberpunk 2077
"""


@pytest.fixture
def umu():
    with patch.object(cyberpunk, "_host_which", return_value="/usr/bin/umu-run"):
        yield


def test_dll_overrides_keep_other_entries_and_win_for_their_dlls():
    assert cyberpunk.with_dll_overrides(None) == "winmm,version=n,b"
    assert cyberpunk.with_dll_overrides("") == "winmm,version=n,b"
    assert cyberpunk.with_dll_overrides("dxgi=n;winmm,d3d11=b;version=d") == "dxgi=n;d3d11=b;winmm,version=n,b"


def test_open_runs_mo2_with_umu_in_heroics_prefix(tmp_path, umu):
    game = make_game(tmp_path)
    instance = make_instance(tmp_path)

    with patch.dict("os.environ", {"WINEDLLOVERRIDES": "dxgi=n"}):
        plan = cyberpunk.build_plan(instance, game)

    assert plan.argv == ["/usr/bin/umu-run", str(instance / "ModOrganizer.exe"), "-p", "Default"]
    assert plan.command == plan.argv[1:]
    assert plan.cwd == instance and plan.proton == PROTON.parent and not plan.play
    assert plan.env["WINEPREFIX"] == plan.env["STEAM_COMPAT_DATA_PATH"] == str(game.prefix)
    assert plan.env["STEAM_COMPAT_INSTALL_PATH"] == str(game.install_path)
    assert plan.env["PROTONPATH"] == str(PROTON.parent)
    assert plan.env["GAMEID"] == "umu-1091500" and plan.env["STORE"] == "gog"
    assert plan.env["WINEDLLOVERRIDES"] == "dxgi=n;winmm,version=n,b"


def test_play_prefers_the_plugins_redmod_executable(tmp_path, umu):
    plan = cyberpunk.build_plan(make_instance(tmp_path, PLUGIN_EXECUTABLES), make_game(tmp_path), play=True)

    assert plan.argv[-4:] == ["Default", "run", "-e", "Cyberpunk 2077"]
    assert plan.target == "Cyberpunk 2077" and plan.play


def test_play_before_mo2_saved_its_executables_runs_the_game_directly(tmp_path, umu):
    game = make_game(tmp_path)
    plan = cyberpunk.build_plan(make_instance(tmp_path), game, play=True)

    exe = game.install_path / "bin/x64/Cyberpunk2077.exe"
    assert plan.argv[-6:] == ["run", "-a", "--launcher-skip -modded", "-c", f"Z:{exe.parent}", f"Z:{exe}"]


def test_plain_wine_runs_the_wine_binary_without_proton_variables(tmp_path):
    plan = cyberpunk.build_plan(make_instance(tmp_path), make_game(tmp_path, wine_type="wine"))

    assert plan.argv[0] == "/usr/bin/wine" and plan.proton is None
    assert "PROTONPATH" not in plan.env and plan.env["WINEDLLOVERRIDES"] == "winmm,version=n,b"


def test_runner_falls_back_to_heroics_own_umu(tmp_path):
    game = make_game(tmp_path)
    bundled = game.config_dir / "tools/runtimes/umu/umu_run.py"
    with patch.object(cyberpunk, "_host_which", return_value=None):
        with pytest.raises(cyberpunk.CyberpunkError, match="umu-launcher"):
            cyberpunk.runner(game)
        bundled.parent.mkdir(parents=True)
        bundled.write_text("")
        assert cyberpunk.runner(game) == ["python3", str(bundled)]


@pytest.mark.parametrize("problem, match", [
    ("no game", "wasn't found in Heroic"),
    ("no prefix", "Launch the game once from Heroic"),
    ("no instance", "Choose Cyberpunk's MO2 instance"),
    ("skyrim instance", "manages Skyrim Special Edition"),
])
def test_launch_problems_say_what_to_do(tmp_path, umu, problem, match):
    game = None if problem == "no game" else make_game(tmp_path, prefix_ready=problem != "no prefix")
    instance = None if problem == "no instance" else make_instance(
        tmp_path, "[General]\ngameName=Skyrim Special Edition\n" if problem == "skyrim instance" else "")

    with pytest.raises(cyberpunk.CyberpunkError, match=match):
        cyberpunk.build_plan(instance, game)


def write_mo2lint_state(tmp_path: Path, *instances: tuple[str, Path]) -> Path:
    state_file = tmp_path / "mo2-lint-state.json"
    state_file.write_text(json.dumps({"instances": [{"game": g, "instance_path": str(p)} for g, p in instances]}))
    return state_file


def test_instance_is_the_chosen_one_else_mo2lints(tmp_path):
    mo2lint = make_instance(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    (other / "ModOrganizer.exe").write_bytes(b"MZ")
    state_file = write_mo2lint_state(tmp_path, ("skyrim_se", other), ("cyberpunk2077", mo2lint))

    with patch("modsync.mo2.installers.mo2lint.mo2lint_state_path", return_value=state_file):
        assert cyberpunk.find_instance() == mo2lint
        with pytest.raises(cyberpunk.CyberpunkError):
            cyberpunk.use_instance(tmp_path / "nothing-here")
        assert cyberpunk.chosen_instance() is None
        assert cyberpunk.use_instance(other) == other.resolve()
        assert cyberpunk.find_instance() == other.resolve()


def test_a_chosen_instance_that_is_missing_is_not_swapped_for_another(tmp_path, umu):
    chosen = make_instance(tmp_path)
    cyberpunk.use_instance(chosen)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ModOrganizer.exe").write_bytes(b"MZ")
    (chosen / "ModOrganizer.exe").unlink()  # its drive isn't mounted
    state_file = write_mo2lint_state(tmp_path, ("cyberpunk2077", elsewhere))

    with patch("modsync.mo2.installers.mo2lint.mo2lint_state_path", return_value=state_file):
        assert cyberpunk.find_instance() == chosen.resolve()
        with pytest.raises(cyberpunk.CyberpunkError, match="no ModOrganizer.exe"):
            cyberpunk.build_plan(cyberpunk.find_instance(), make_game(tmp_path))


def test_the_choice_survives_state_file_writes(tmp_path):
    """Kept apart from state.json, which a ModSync from before Cyberpunk
    rewrites without fields it doesn't know."""
    from modsync.state import State

    cyberpunk.use_instance(make_instance(tmp_path))
    State.update(home_game="daggerfall")

    assert cyberpunk.chosen_instance() == (tmp_path / "ModOrganizer2-Cyberpunk2077").resolve()
    assert "cyberpunk" not in State.path().read_text()


def test_cli_launch_reports_a_missing_chosen_instance(tmp_path, capsys):
    from modsync.__main__ import main

    chosen = make_instance(tmp_path)
    cyberpunk.use_instance(chosen)
    (chosen / "ModOrganizer.exe").unlink()

    with patch.object(cyberpunk, "find_game", return_value=make_game(tmp_path)):
        assert main(["cyberpunk", "play"]) == 1
    assert "no ModOrganizer.exe" in capsys.readouterr().out
