"""Heroic's Play button through MO2 (modsync.heroic.hook): the rendered script,
run the way Heroic runs a wrapper, and its place in the game's settings."""

import json
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from modsync.heroic import hook
from modsync.heroic.library import GogGame, WineVersion

APP = "1423049311"

RECORDER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["RECORD"], "a") as f:
    f.write(json.dumps({"name": os.path.basename(sys.argv[0]), "argv": sys.argv[1:], "cwd": os.getcwd(),
                        "overrides": os.environ.get("WINEDLLOVERRIDES")}) + "\\n")
"""


# Another wrapper after the hook (gamemoderun, mangohud): records itself, runs the rest.
WRAPPER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["RECORD"], "a") as f:
    f.write(json.dumps({"name": "gamemoderun", "argv": sys.argv[1:]}) + "\\n")
os.execv(sys.argv[1], sys.argv[1:])
"""


def executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def make_heroic(tmp_path: Path, *, wrappers=(), flatpak=False) -> GogGame:
    config_dir = tmp_path / (".var/app/com.heroicgameslauncher.hgl/config/heroic" if flatpak else ".config/heroic")
    (config_dir / "GamesConfig").mkdir(parents=True)
    (config_dir / "GamesConfig" / f"{APP}.json").write_text(json.dumps(
        {APP: {"showFps": False, "wrapperOptions": list(wrappers)}, "version": "v0", "explicit": True}, indent=2))
    return GogGame(APP, tmp_path / "Cyberpunk 2077", "2.5", "1", True, config_dir,
                   prefix=tmp_path / "prefix", wine=WineVersion(Path("/opt/proton/proton"), "p", "proton"))


def render(game: GogGame, instance: Path) -> str:
    return hook.render(game, game_name="Cyberpunk 2077", instance=instance, play_args=["run", "-e", "Cyberpunk 2077"],
                       open_args=[], disable_command="modsync cyberpunk hook disable")


@pytest.fixture
def scene(tmp_path):
    instance = tmp_path / "MO2 Cyberpunk"  # a space, to check the quoting
    instance.mkdir()
    (instance / "ModOrganizer.exe").write_bytes(b"MZ")
    game = make_heroic(tmp_path)
    script = executable(tmp_path / "play.sh", render(game, instance))
    umu = executable(tmp_path / "bin/umu-run", RECORDER)
    other = executable(tmp_path / "bin/gamemoderun", WRAPPER)
    return {"tmp": tmp_path, "instance": instance, "script": script, "umu": umu, "other": other,
            "exe": str(tmp_path / "Cyberpunk 2077/REDprelauncher.exe"), "record": tmp_path / "record.jsonl"}


def run(scene, *args, env=None):
    result = subprocess.run(["bash", str(scene["script"]), hook.MARKER, APP, *args], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "RECORD": str(scene["record"]), **(env or {})}, timeout=30)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in scene["record"].read_text().splitlines()] if scene["record"].exists() else []
    return calls, result.stderr


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def test_play_runs_mo2_with_heroics_runner_in_place_of_the_game(scene):
    calls, said = run(scene, str(scene["umu"]), scene["exe"], "--some-arg",
                      env={"WINEDLLOVERRIDES": "dxgi=n;winmm,d3d11=b;version="})

    (call,) = calls
    assert call["name"] == "umu-run"
    assert call["argv"] == [f"{scene['instance']}/ModOrganizer.exe", "run", "-e", "Cyberpunk 2077"]
    assert call["cwd"] == str(scene["instance"])
    assert call["overrides"] == "dxgi=n;d3d11=b;winmm,version=n,b"
    assert "playing through Mod Organizer 2" in said


def test_wrappers_after_the_hook_are_kept(scene):
    calls, _ = run(scene, str(scene["other"]), str(scene["umu"]), scene["exe"])

    assert [c["name"] for c in calls] == ["gamemoderun", "umu-run"]
    assert calls[1]["argv"][0].endswith("ModOrganizer.exe") and calls[1]["overrides"] == "winmm,version=n,b"


def test_mo2lints_launch_option_opens_mo2(scene):
    calls, said = run(scene, str(scene["umu"]), str(scene["tmp"] / "Cyberpunk 2077/mo2-redirector.exe"))

    assert calls[0]["argv"] == [f"{scene['instance']}/ModOrganizer.exe"]
    assert "opening Mod Organizer 2" in said


@pytest.mark.parametrize("case", ["original requested", "instance missing"])
def test_the_game_starts_as_before_when_mo2_cant(scene, case):
    env = {"MODSYNC_ORIGINAL_GAME": "1"} if case == "original requested" else {}
    if case == "instance missing":
        (scene["instance"] / "ModOrganizer.exe").unlink()

    calls, said = run(scene, str(scene["umu"]), scene["exe"], "--arg", env=env)

    assert calls[0]["argv"] == [scene["exe"], "--arg"]
    assert calls[0]["overrides"] == "winmm,version=n,b"  # set either way: harmless, and mods may be in the folder
    assert ("MODSYNC_ORIGINAL_GAME" in said) if case == "original requested" else ("isn't there" in said)


# --- the game's settings in Heroic ---------------------------------------------


@pytest.fixture
def closed_heroic():
    with patch.object(hook, "heroic_running", return_value=False):
        yield


def wrappers(game: GogGame) -> list:
    return json.loads((game.config_dir / "GamesConfig" / f"{APP}.json").read_text())[APP]["wrapperOptions"]


def test_enable_puts_the_hook_first_and_disable_takes_only_it_out(tmp_path, closed_heroic):
    mangohud = {"exe": "/usr/bin/mangohud", "args": ""}
    game = make_heroic(tmp_path, wrappers=[mangohud])
    text = render(game, tmp_path / "MO2")

    hook.enable(game, text, tmp_path / "MO2")
    hook.enable(game, text, tmp_path / "MO2")  # again: still one

    assert wrappers(game) == [{"exe": str(hook.script_path(game)), "args": f"{hook.MARKER} {APP}"}, mangohud]
    assert hook.script_path(game).read_text() == text and hook.script_path(game).stat().st_mode & stat.S_IXUSR
    settings = json.loads((game.config_dir / "GamesConfig" / f"{APP}.json").read_text())
    assert settings["explicit"] is True and settings[APP]["showFps"] is False
    assert hook.status(game, text) == hook.HookStatus(True, True, hook.script_path(game))

    hook.disable(game)
    assert wrappers(game) == [mangohud]
    assert not hook.script_path(game).exists()
    assert not hook.status(game, text).enabled


def test_settings_are_left_alone_while_heroic_runs(tmp_path):
    game = make_heroic(tmp_path)
    with patch.object(hook, "heroic_running", return_value=True):
        with pytest.raises(hook.HookError, match="Close Heroic"):
            hook.enable(game, "script", tmp_path / "MO2")
        with pytest.raises(hook.HookError, match="Close Heroic"):
            hook.disable(game)
    assert wrappers(game) == []


def test_a_game_heroic_never_launched_has_no_settings_to_hook(tmp_path, closed_heroic):
    game = make_heroic(tmp_path)
    (game.config_dir / "GamesConfig" / f"{APP}.json").unlink()
    with pytest.raises(hook.HookError, match="Launch it once from Heroic"):
        hook.enable(game, "script", tmp_path / "MO2")


def test_refresh_rewrites_an_enabled_out_of_date_script_only(tmp_path, closed_heroic):
    game = make_heroic(tmp_path)
    assert not hook.refresh(game, "new")  # not enabled: nothing to do
    assert not hook.script_path(game).exists()

    hook.enable(game, "old", tmp_path / "MO2")
    assert hook.status(game, "new").summary().startswith("on, but out of date")
    with patch.object(hook, "heroic_running", return_value=True):  # needs no restart
        assert hook.refresh(game, "new")
    assert hook.script_path(game).read_text() == "new"
    assert not hook.refresh(game, "new")


def test_heroics_flatpak_is_given_access_to_an_instance_it_cant_see(tmp_path, closed_heroic, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    game = make_heroic(tmp_path, flatpak=True)
    calls = []

    def run_host(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch.object(hook.background, "run_host", side_effect=run_host):
        done = hook.enable(game, "script", tmp_path / "ModOrganizer2-cyberpunk2077")
        hook.enable(game, "script", tmp_path / "Games/Heroic/ModOrganizer2-cyberpunk2077")

    assert calls == [["flatpak", "override", "--user", f"--filesystem={tmp_path}/ModOrganizer2-cyberpunk2077",
                      "com.heroicgameslauncher.hgl"]]
    assert "Gave Heroic's Flatpak access" in done[0]


def test_the_cyberpunk_script_plays_mo2s_executable_without_naming_a_profile(tmp_path):
    from modsync import cyberpunk
    from tests.test_cyberpunk import PLUGIN_EXECUTABLES, make_game, make_instance

    game = make_game(tmp_path)
    text = cyberpunk.hook_script(game, make_instance(tmp_path, PLUGIN_EXECUTABLES))

    assert "play=(run -e 'Cyberpunk 2077')" in text and "-p" not in text.split("play=")[1].split("\n")[0]
    assert "@@" not in text
