"""Heroic's Play button through MO2 (modsync.heroic.hook): the rendered script,
run the way Heroic runs a wrapper, and its place in the game's settings."""

import json
import re
import shlex
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

# What MO2's Cyberpunk plugin saves (raw: the backslashes are as MO2 writes them).
EXECUTABLES = r"""[General]
gameName=Cyberpunk 2077

[customExecutables]
size=3
1\arguments="--launcher-skip "
1\binary=Z:/games/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe
1\title=Cyberpunk 2077 - skip REDmod deploy
2\arguments=--launcher-skip -modded
2\binary=Z:/games/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe
2\title=My Cyberpunk, renamed
3\arguments=deploy
3\binary=Z:/games/Cyberpunk 2077/tools/redmod/bin/redMod.exe
3\title=Manually deploy REDmod
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
    return GogGame(APP, tmp_path / "Games/Cyberpunk 2077", "2.5", "1", True, config_dir,
                   prefix=tmp_path / "prefix", wine=WineVersion(Path("/opt/proton/proton"), "p", "proton"))


def render(game: GogGame, instance: Path) -> str:
    return hook.render(game, game_name="Cyberpunk 2077", instance=instance, game_exe="Cyberpunk2077.exe",
                       prefer_argument="-modded", direct_args=["run", "-a", "--launcher-skip -modded", "Z:/x.exe"],
                       disable_command="modsync cyberpunk hook disable")


@pytest.fixture
def scene(tmp_path):
    instance = tmp_path / "MO2 Cyberpunk"  # a space, to check the quoting
    instance.mkdir()
    (instance / "ModOrganizer.exe").write_bytes(b"MZ")
    (instance / "ModOrganizer.ini").write_text("[General]\ngameName=Cyberpunk 2077\n")
    game = make_heroic(tmp_path)
    script = executable(tmp_path / "play.sh", render(game, instance))
    return {"tmp": tmp_path, "instance": instance, "script": script, "game_dir": game.install_path,
            "umu": executable(tmp_path / "bin/umu-run", RECORDER),
            "other": executable(tmp_path / "bin/gamemoderun", WRAPPER),
            "exe": str(game.install_path / "REDprelauncher.exe"), "record": tmp_path / "record.jsonl"}


def run(scene, *args, env=None):
    result = subprocess.run(["bash", str(scene["script"]), hook.MARKER, APP, *args], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin", "RECORD": str(scene["record"]), **(env or {})}, timeout=30)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in scene["record"].read_text().splitlines()] if scene["record"].exists() else []
    return calls, result.stderr


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


def test_play_runs_mo2s_redmod_executable_with_heroics_runner(scene):
    (scene["instance"] / "ModOrganizer.ini").write_text(EXECUTABLES.replace("\n", "\r\n"))  # CRLF too
    calls, said = run(scene, str(scene["umu"]), scene["exe"], "--some-arg",
                      env={"WINEDLLOVERRIDES": "dxgi=n;winmm,d3d11=b;version="})

    (call,) = calls
    assert call["name"] == "umu-run"
    assert call["argv"] == [f"{scene['instance']}/ModOrganizer.exe", "run", "-e", "My Cyberpunk, renamed"]
    assert call["cwd"] == str(scene["instance"])
    assert call["overrides"] == "dxgi=n;d3d11=b;winmm,version=n,b"
    assert "playing through Mod Organizer 2" in said


def test_before_mo2_saved_its_executables_it_runs_the_game_directly(scene):
    calls, _ = run(scene, str(scene["umu"]), scene["exe"])

    assert calls[0]["argv"][1:] == ["run", "-a", "--launcher-skip -modded", "Z:/x.exe"]


def test_wrappers_after_the_hook_are_kept(scene):
    calls, _ = run(scene, str(scene["other"]), str(scene["umu"]), scene["exe"])

    assert [c["name"] for c in calls] == ["gamemoderun", "umu-run"]
    assert calls[1]["argv"][0].endswith("ModOrganizer.exe") and calls[1]["overrides"] == "winmm,version=n,b"


def test_a_wrappers_own_exe_argument_is_not_taken_for_the_game(scene):
    """Only a program in the game's folder is the game: a wrapper's own .exe
    argument stays where it is."""
    picky = executable(scene["tmp"] / "bin/picky-wrapper", RECORDER)  # records; doesn't run the rest
    calls, _ = run(scene, str(picky), "--rules", "/config/rules.exe", str(scene["umu"]), scene["exe"])

    assert calls[0]["name"] == "picky-wrapper"
    assert calls[0]["argv"] == ["--rules", "/config/rules.exe", str(scene["umu"]), f"{scene['instance']}/ModOrganizer.exe",
                                "run", "-a", "--launcher-skip -modded", "Z:/x.exe"]


@pytest.mark.parametrize("redirector", ["{game}/mo2-redirector.exe", "Z:{game_win}\\mo2-redirector.exe"])
def test_mo2lints_launch_option_opens_mo2(scene, redirector):
    game_win = str(scene["game_dir"]).replace("/", "\\")
    calls, said = run(scene, str(scene["umu"]), redirector.format(game=scene["game_dir"], game_win=game_win))

    assert calls[0]["argv"] == [f"{scene['instance']}/ModOrganizer.exe"]
    assert "opening Mod Organizer 2" in said


LINK = "nxm://cyberpunk2077/mods/2987/files/168466?key=k&expires=1&user_id=2"


def note(scene) -> Path:
    return scene["tmp"] / ".config/heroic/modsync" / f"download-{APP}.txt"


@pytest.mark.parametrize("program", ["exe", "redirector"])
def test_a_download_link_left_by_modsync_opens_mo2_with_it_once(scene, program):
    (scene["instance"] / "ModOrganizer.ini").write_text(EXECUTABLES)
    note(scene).parent.mkdir(parents=True)
    note(scene).write_text(LINK + "\n")
    exe = scene["exe"] if program == "exe" else str(scene["game_dir"] / "mo2-redirector.exe")

    calls, said = run(scene, str(scene["umu"]), exe)

    assert calls[0]["argv"] == [f"{scene['instance']}/ModOrganizer.exe", LINK]
    assert calls[0]["overrides"] == "winmm,version=n,b"
    assert "for a Nexus Mods download" in said and "key=" not in said
    assert not note(scene).exists()
    calls, _ = run(scene, str(scene["umu"]), scene["exe"])  # the next Play plays
    assert calls[-1]["argv"][1:] == ["run", "-e", "My Cyberpunk, renamed"]


@pytest.mark.parametrize("case", ["stale", "not a link"])
def test_a_stale_or_odd_note_is_dropped_and_the_game_plays(scene, case):
    import os

    note(scene).parent.mkdir(parents=True)
    note(scene).write_text((LINK if case == "stale" else "--help") + "\n")
    if case == "stale":
        old = note(scene).stat().st_mtime - hook.DOWNLOAD_TTL - 5
        os.utime(note(scene), (old, old))

    calls, said = run(scene, str(scene["umu"]), scene["exe"])

    assert calls[0]["argv"][1] == "run"
    assert "ignoring a download link" in said
    assert not note(scene).exists()


@pytest.mark.parametrize("case", ["original requested", "instance missing", "no game program"])
def test_the_launch_runs_as_it_was_when_mo2_cant_take_it(scene, case):
    env = {"MODSYNC_ORIGINAL_GAME": "1"} if case == "original requested" else {}
    if case == "instance missing":
        (scene["instance"] / "ModOrganizer.exe").unlink()
    exe = "/elsewhere/Cyberpunk2077.exe" if case == "no game program" else scene["exe"]

    calls, said = run(scene, str(scene["umu"]), exe, "--arg", env=env)

    assert calls[0]["argv"] == [exe, "--arg"]
    assert calls[0]["overrides"] == "winmm,version=n,b"  # set either way: harmless, and mods may be in the folder
    assert {"original requested": "MODSYNC_ORIGINAL_GAME", "instance missing": "isn't there",
            "no game program": "running it as it was"}[case] in said


def test_heroics_own_splitting_of_the_wrapper_delivers_the_marker(scene, tmp_path):
    """Heroic joins wrappers with shlex and gogdl splits them again; a script
    path with spaces still arrives whole."""
    (tmp_path / "Heroic config").mkdir()
    script = Path(shutil.copy(scene["script"], tmp_path / "Heroic config" / "play.sh"))
    script.chmod(0o755)
    wrapper = shlex.join([str(script), *f"{hook.MARKER} {APP}".split()]) + " " + shlex.quote(str(scene["umu"]))
    subprocess.run([*shlex.split(wrapper), scene["exe"]], env={"PATH": "/usr/bin:/bin", "RECORD": str(scene["record"])},
                   check=True, capture_output=True, timeout=30)
    assert json.loads(scene["record"].read_text())["argv"][0] == f"{scene['instance']}/ModOrganizer.exe"


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
    assert not hook.script_path(game).parent.exists()
    assert not hook.status(game, text).enabled


def test_settings_are_left_alone_while_heroic_runs(tmp_path):
    game = make_heroic(tmp_path)
    with patch.object(hook, "heroic_running", return_value=True):
        with pytest.raises(hook.HookError, match="Close Heroic"):
            hook.enable(game, "script", tmp_path / "MO2")
        with pytest.raises(hook.HookError, match="Close Heroic"):
            hook.disable(game)
    assert wrappers(game) == []


def test_disable_keeps_the_script_when_heroics_settings_cant_be_read(tmp_path, closed_heroic):
    game = make_heroic(tmp_path)
    hook.enable(game, "script", tmp_path / "MO2")
    (game.config_dir / "GamesConfig" / f"{APP}.json").write_text("{broken")

    with pytest.raises(hook.HookError, match="can't be read"):
        hook.disable(game)
    assert hook.script_path(game).exists()


def test_a_game_heroic_never_launched_has_no_settings_to_hook(tmp_path, closed_heroic):
    game = make_heroic(tmp_path)
    (game.config_dir / "GamesConfig" / f"{APP}.json").unlink()
    with pytest.raises(hook.NoSettings, match="Launch it once from Heroic"):
        hook.enable(game, "script", tmp_path / "MO2")
    assert hook.disable(game)  # nothing to take out


def test_refresh_rewrites_an_enabled_out_of_date_script_only(tmp_path, closed_heroic):
    game = make_heroic(tmp_path)
    assert hook.refresh(game, "new", tmp_path / "MO2") == []  # not enabled: nothing to do
    assert not hook.script_path(game).exists()

    hook.enable(game, "old", tmp_path / "MO2")
    assert hook.status(game, "new").summary().startswith("on, but out of date")
    with patch.object(hook, "heroic_running", return_value=True):  # needs no restart
        assert hook.refresh(game, "new", tmp_path / "MO2") == ["Updated Heroic's Play hook."]
    assert hook.script_path(game).read_text() == "new"
    assert hook.refresh(game, "new", tmp_path / "MO2") == []


def flatpak_host(calls):
    def run_host(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return patch.object(hook.background, "run_host", side_effect=run_host)


def test_heroics_flatpak_is_given_access_to_an_instance_it_cant_see(tmp_path, closed_heroic, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    game = make_heroic(tmp_path, flatpak=True)
    calls = []

    with flatpak_host(calls):
        done = hook.enable(game, "script", tmp_path / "ModOrganizer2-cyberpunk2077")
        hook.enable(game, "script", tmp_path / "Games/Heroic/ModOrganizer2-cyberpunk2077")
        hook.enable(game, "script", tmp_path / "Documents/MO2")  # xdg-documents may be named otherwise

    assert calls == [["flatpak", "override", "--user", f"--filesystem={tmp_path}/{d}", "com.heroicgameslauncher.hgl"]
                     for d in ("ModOrganizer2-cyberpunk2077", "Documents/MO2")]
    assert "Gave Heroic's Flatpak access" in done[0]


def test_switching_instances_gives_heroics_flatpak_access_to_the_new_one(tmp_path, closed_heroic, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    game = make_heroic(tmp_path, flatpak=True)
    calls = []
    with flatpak_host(calls):
        hook.enable(game, "script A", tmp_path / "Games/Heroic/A")
        notes = hook.refresh(game, "script B", tmp_path / "B")

    assert calls == [["flatpak", "override", "--user", f"--filesystem={tmp_path}/B", "com.heroicgameslauncher.hgl"]]
    assert notes == [f"Gave Heroic's Flatpak access to {tmp_path}/B; restart Heroic before playing.",
                     "Updated Heroic's Play hook."]


def test_flatpak_visibility_compares_resolved_paths(tmp_path, monkeypatch):
    real_home = tmp_path / "var/home/deck"
    (real_home / "Games/Heroic").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    (tmp_path / "home/deck").symlink_to(real_home)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home/deck")

    assert hook.flatpak_sees(real_home / "Games/Heroic/MO2")
    assert not hook.flatpak_sees(real_home / "MO2")


def test_the_heroic_check_never_matches_its_own_command_line():
    """From ModSync's Flatpak, flatpak-spawn's command line carries the pattern."""
    pattern = re.compile(hook.HEROIC_PROCESS)
    assert not pattern.search(f"flatpak-spawn --host pgrep -f {hook.HEROIC_PROCESS}")
    for heroic in ("/usr/lib/electron43/electron /usr/lib/heroic-games-launcher/app.asar", "/opt/Heroic/heroic",
                   "/tmp/.mount_HeroicAbc/heroic", "/app/bin/heroic/heroic --no-sandbox"):
        assert pattern.search(heroic), heroic


def test_a_failed_heroic_check_is_an_error_not_closed():
    with patch.object(hook.background, "run_host", return_value=subprocess.CompletedProcess([], 2, "", "bad")), \
            pytest.raises(hook.HookError, match="Couldn't check"):
        hook.heroic_running()
    with patch.object(hook.background, "run_host", side_effect=FileNotFoundError("pgrep")), \
            pytest.raises(hook.HookError, match="Couldn't check"):
        hook.heroic_running()
    with patch.object(hook.background, "run_host", return_value=subprocess.CompletedProcess([], 1, "", "")):
        assert not hook.heroic_running()


def test_the_cyberpunk_script_names_the_game_folder_and_no_profile(tmp_path):
    from modsync import cyberpunk
    from tests.test_cyberpunk import make_game, make_instance

    game = make_game(tmp_path)
    text = cyberpunk.hook_script(game, make_instance(tmp_path))

    assert f"game_dir={shlex.quote(str(game.install_path))}" in text
    assert "game_exe=Cyberpunk2077.exe" in text and "prefer=-modded" in text
    assert "@@" not in text and " -p " not in text


@pytest.mark.parametrize("flatpak", [False, True])
def test_launch_asks_the_heroic_that_has_the_game(tmp_path, flatpak):
    game = make_heroic(tmp_path, flatpak=flatpak)
    with patch.object(hook.subprocess, "Popen") as popen, patch.object(hook.background, "in_flatpak", return_value=False):
        hook.launch(game)
    url = f"heroic://launch?appName={APP}&runner=gog"
    assert popen.call_args.args[0] == (["flatpak", "run", hook.FLATPAK_ID, url] if flatpak else ["xdg-open", url])


def test_disable_drops_a_download_link_left_behind(tmp_path, closed_heroic):
    game = make_heroic(tmp_path)
    hook.enable(game, "script", tmp_path / "MO2")
    hook.leave_download(game, "nxm://x")
    hook.disable(game)
    assert not hook.download_note(game).exists()
