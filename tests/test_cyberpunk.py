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


@pytest.fixture(autouse=True)
def _no_chosen_instance():
    """The scratch config folder lasts the whole session; a choice one test
    makes mustn't decide the next."""
    cyberpunk._settings_path().unlink(missing_ok=True)
    yield
    cyberpunk._settings_path().unlink(missing_ok=True)


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


@pytest.mark.parametrize("play", [True, False])
def test_play_and_open_go_through_heroic_while_its_hook_is_on(tmp_path, play):
    """Heroic's launch, so it syncs GOG cloud saves; the note makes the hook
    play or open MO2 whichever of the game's launch options Heroic runs."""
    from modsync.mo2.launch import Launcher

    with Downloading(tmp_path, hook_on=True) as d:
        message = cyberpunk.launch(Launcher(), play=play)
        assert message == ("Starting Cyberpunk 2077 through Heroic…" if play else
                           "Opening Mod Organizer 2 through Heroic…")
        note = cyberpunk.heroic_hook.launch_note(d.game).read_text().strip()
        assert note == (cyberpunk.heroic_hook.PLAY if play else cyberpunk.heroic_hook.OPEN)
        assert cyberpunk.heroic_hook.launch.call_args.args == (d.game,)
        Launcher.start.assert_not_called()
        with pytest.raises(cyberpunk.CyberpunkError, match="already open"):  # Heroic would ignore a second
            cyberpunk.launch(Launcher(), play=play)
        assert cyberpunk.heroic_hook.launch.call_count == 1


def test_without_the_hook_modsync_starts_mo2_itself(tmp_path):
    from modsync.mo2.launch import Launcher

    with Downloading(tmp_path) as d:
        assert cyberpunk.launch(Launcher(), play=False) == "Starting Mod Organizer 2…"
        (plan,) = Launcher.start.call_args.args
        assert plan.command[0] == str(d.instance / "ModOrganizer.exe")
        cyberpunk.heroic_hook.launch.assert_not_called()


def test_a_link_while_play_starts_mo2_through_heroic_waits_for_it(tmp_path):
    from modsync.mo2.launch import Launcher

    with Downloading(tmp_path, hook_on=True):
        cyberpunk.launch(Launcher(), play=True)
        cyberpunk.mo2_running.side_effect = [False, True, True]
        assert cyberpunk.download(LINK, poll=0) == "Passed the download to Mod Organizer 2."
        assert cyberpunk.heroic_hook.launch.call_count == 1
        cyberpunk.heroic_hook.leave_download.assert_not_called()


def test_a_launch_heroic_refused_leaves_no_note(tmp_path):
    from modsync.mo2.launch import Launcher

    with Downloading(tmp_path, hook_on=True) as d:
        cyberpunk.heroic_hook.launch.side_effect = cyberpunk.heroic_hook.HookError("no Heroic")
        with pytest.raises(cyberpunk.heroic_hook.HookError):
            cyberpunk.launch(Launcher(), play=True)
        assert not cyberpunk.heroic_hook.launch_note(d.game).exists()
        cyberpunk.heroic_hook.launch.side_effect = None
        assert "through Heroic" in cyberpunk.launch(Launcher(), play=True)  # nothing recorded to wait for


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


# --- installing MO2 -----------------------------------------------------------


class FakeRun:
    """Stands in for run_in_prefix: answers list-installed, records the rest."""

    def __init__(self, installed="", results=()):
        self.installed, self.results, self.calls = installed, list(results), []

    def __call__(self, game, args, **_kw):
        from subprocess import CompletedProcess

        self.calls.append(args)
        if args[-1] == "list-installed":
            return CompletedProcess(args, 0, self.installed, "")
        return self.results.pop(0) if self.results else CompletedProcess(args, 0, "", "")


def test_prepare_prefix_runs_only_the_missing_verbs_through_umu(tmp_path):
    fake = FakeRun(installed="win10\narial\n")
    with patch.object(cyberpunk, "run_in_prefix", fake), \
            patch("modsync.mo2.vcruntime.prefix_runtime_problems", return_value=[]):
        cyberpunk.prepare_prefix(make_game(tmp_path))

    assert fake.calls == [["winetricks", "-q", "list-installed"],
                          ["winetricks", "-q", "fontsmooth=rgb", "xaudio2_7=native", "d3dcompiler_47"]]


def test_prepare_prefix_says_why_winetricks_failed(tmp_path):
    from subprocess import CompletedProcess

    fake = FakeRun(results=[CompletedProcess([], 1, "", "INFO: umu\nERROR: no network\n")])
    with patch.object(cyberpunk, "run_in_prefix", fake), pytest.raises(cyberpunk.CyberpunkError, match="no network"):
        cyberpunk.prepare_prefix(make_game(tmp_path))


def test_prepare_prefix_installs_the_vc_runtime_only_when_needed(tmp_path):
    from subprocess import CompletedProcess

    installer = tmp_path / "VC_redist.x64.exe"
    game = make_game(tmp_path)
    # 3010 (installed, reboot requested) as Linux sees it: its low byte.
    fake = FakeRun(installed="\n".join(cyberpunk.TRICKS), results=[CompletedProcess([], 3010 & 0xFF, "", "")])
    with patch.object(cyberpunk, "run_in_prefix", fake), \
            patch("modsync.mo2.vcruntime.prefix_runtime_problems", side_effect=[["msvcp140.dll is 14.0.24215.1"], []]), \
            patch("modsync.mo2.vcruntime.redist_installer", return_value=installer), \
            patch("modsync.mo2.vcruntime.backup_prefix_runtime", return_value=tmp_path / "backup") as backup:
        cyberpunk.prepare_prefix(game)

    assert fake.calls[-1] == [str(installer), "/install", "/quiet", "/norestart"]
    assert backup.call_args.args[0] == game.drive_c / "windows" / "system32"

    fake = FakeRun(installed="\n".join(cyberpunk.TRICKS), results=[CompletedProcess([], 0, "", "")])
    with patch.object(cyberpunk, "run_in_prefix", fake), \
            patch("modsync.mo2.vcruntime.prefix_runtime_problems", return_value=["msvcp140.dll is missing"]), \
            patch("modsync.mo2.vcruntime.redist_installer", return_value=installer), \
            patch("modsync.mo2.vcruntime.backup_prefix_runtime", return_value=tmp_path / "backup"), \
            pytest.raises(cyberpunk.CyberpunkError, match="still has msvcp140.dll is missing"):
        cyberpunk.prepare_prefix(game)

    fake = FakeRun(installed="\n".join(cyberpunk.TRICKS), results=[CompletedProcess([], 1603 & 0xFF, "", "")])
    with patch.object(cyberpunk, "run_in_prefix", fake), \
            patch("modsync.mo2.vcruntime.prefix_runtime_problems", return_value=["msvcp140.dll is missing"]), \
            patch("modsync.mo2.vcruntime.redist_installer", return_value=installer), \
            patch("modsync.mo2.vcruntime.backup_prefix_runtime", return_value=tmp_path / "backup"), \
            pytest.raises(cyberpunk.CyberpunkError, match="code 67"):
        cyberpunk.prepare_prefix(game)


def test_plain_wine_uses_the_hosts_winetricks_with_the_games_wine(tmp_path):
    game = make_game(tmp_path, wine_type="wine")
    with patch.object(cyberpunk, "_host_which", return_value="/usr/bin/winetricks"):
        assert cyberpunk.winetricks_command(game, ["win10"]) == (
            ["/usr/bin/winetricks", "-q", "win10"], {"WINE": "/usr/bin/wine"})
    with patch.object(cyberpunk, "_host_which", return_value=None), pytest.raises(cyberpunk.CyberpunkError):
        cyberpunk.winetricks_command(game, ["win10"])


def make_rootbuilder_zip(path: Path) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("rootbuilder/", "")
        archive.writestr("rootbuilder/__init__.py", "# plugin")
        archive.writestr("rootbuilder/plugin/rootbuilder/core.py", "")
        archive.writestr("rootbuilder/../../escaped.txt", "nope")
        archive.writestr("README.md", "not a plugin file")
    return path


def test_rootbuilder_goes_into_plugins_set_up_for_cyberpunk(tmp_path):
    instance = make_instance(tmp_path)
    with open(instance / "ModOrganizer.ini", "a") as f:
        f.write("\n[Plugins]\nRootBuilder\\linkmode=true\n")  # chosen in MO2: kept
    with patch.object(cyberpunk, "_rootbuilder_zip", return_value=make_rootbuilder_zip(tmp_path / "rb.zip")):
        cyberpunk.install_rootbuilder(instance)

    assert (instance / "plugins/rootbuilder/__init__.py").read_text() == "# plugin"
    assert (instance / "plugins/rootbuilder/plugin/rootbuilder/core.py").exists()
    assert not (tmp_path / "escaped.txt").exists() and not (instance / "plugins/README.md").exists()
    from modsync.mo2 import ini

    plugins = ini.read_ini(instance / "ModOrganizer.ini")["Plugins"]
    assert plugins["RootBuilder\\linkmode"] == "true"
    assert plugins["RootBuilder\\autobuild"] == "true" and plugins["RootBuilder\\usvfsmode"] == "false"
    assert plugins["RootBuilder\\exclusions"] == '"archive,setup_redlauncher.exe,tools"'
    assert plugins["Cyberpunk%202077%20Support%20Plugin\\configure_RootBuilder"] == "false"


def test_rootbuilder_download_must_match_its_checksum(tmp_path):
    import io

    with patch("urllib.request.urlopen", return_value=io.BytesIO(b"not the release")), \
            pytest.raises(cyberpunk.CyberpunkError, match="checksum"):
        cyberpunk._rootbuilder_zip()
    from modsync import config

    assert not list((config.data_dir() / "downloads").glob("rootbuilder.*"))


class FakeBackend:
    def __init__(self, tmp_path):
        self.tmp_path, self.calls = tmp_path, []

    def available(self):
        return True, ""

    def install(self, game, dest, **kw):
        from modsync.mo2.installers.base import InstallResult

        self.calls.append((game, dest, kw))
        return InstallResult(True, 0, make_instance(self.tmp_path))


def test_install_mo2_runs_mo2lint_for_gog_then_the_prefix_and_rootbuilder(tmp_path):
    backend = FakeBackend(tmp_path)
    game = make_game(tmp_path)
    with patch.object(cyberpunk, "find_game", return_value=game), \
            patch.object(cyberpunk, "game_running", return_value=False), \
            patch.object(cyberpunk, "prepare_prefix") as prepare, \
            patch.object(cyberpunk, "install_rootbuilder") as rootbuilder:
        instance = cyberpunk.install_mo2(tmp_path / "dest", backend=backend)

    (played, dest, kw), = backend.calls
    assert played.mo2lint_key == "cyberpunk2077" and dest == tmp_path / "dest"
    assert kw["launcher"] == "gog" and kw["env"] == cyberpunk.MO2LINT_ENV
    prepare.assert_called_once_with(game, None)
    rootbuilder.assert_called_once_with(instance)
    assert cyberpunk.chosen_instance() == instance


def test_install_again_after_a_failed_step_finishes_the_setup(tmp_path):
    backend = FakeBackend(tmp_path)
    game = make_game(tmp_path)
    with patch.object(cyberpunk, "find_game", return_value=game), \
            patch.object(cyberpunk, "game_running", return_value=False), \
            patch.object(cyberpunk, "install_rootbuilder"):
        with patch.object(cyberpunk, "prepare_prefix", side_effect=cyberpunk.CyberpunkError("winetricks exited with code 1.")), \
                pytest.raises(cyberpunk.CyberpunkError, match="run the install again"):
            cyberpunk.install_mo2(tmp_path / "ModOrganizer2-Cyberpunk2077", backend=backend)
        assert cyberpunk.chosen_instance() is None
        with patch.object(cyberpunk, "prepare_prefix") as prepare:
            instance = cyberpunk.install_mo2(tmp_path / "ModOrganizer2-Cyberpunk2077", backend=backend)

    assert len(backend.calls) == 1  # MO2-LINT ran once; the retry only finished the setup
    prepare.assert_called_once()
    assert cyberpunk.chosen_instance() == instance


def test_install_over_another_games_instance_is_refused(tmp_path):
    other = make_instance(tmp_path, "[General]\ngameName=Skyrim Special Edition\n")
    with patch.object(cyberpunk, "find_game", return_value=make_game(tmp_path)), \
            patch.object(cyberpunk, "game_running", return_value=False), \
            pytest.raises(cyberpunk.CyberpunkError, match="manages Skyrim"):
        cyberpunk.install_mo2(other, backend=FakeBackend(tmp_path))


@pytest.mark.parametrize("running, prefix_ready, match", [
    (True, True, "Close Cyberpunk 2077"),
    (False, False, "Launch the game once"),
])
def test_install_mo2_refuses_when_the_prefix_is_busy_or_missing(tmp_path, running, prefix_ready, match):
    backend = FakeBackend(tmp_path)
    with patch.object(cyberpunk, "find_game", return_value=make_game(tmp_path, prefix_ready=prefix_ready)), \
            patch.object(cyberpunk, "game_running", return_value=running), \
            pytest.raises(cyberpunk.CyberpunkError, match=match):
        cyberpunk.install_mo2(tmp_path / "dest", backend=backend)
    assert backend.calls == []


def test_game_running_matches_wine_processes_not_shells():
    import re
    from subprocess import CompletedProcess

    with patch.object(cyberpunk.background, "run_host", return_value=CompletedProcess([], 0)) as run:
        assert cyberpunk.game_running()
    pattern = re.compile(run.call_args.args[0][-1])
    assert pattern.search(r"Z:\home\deck\Games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe --launcher-skip -modded")
    assert pattern.search(r"X:\ModOrganizer2-cyberpunk2077\ModOrganizer.exe -p Default run -e Cyberpunk 2077")
    assert not pattern.search("zsh -c pgrep -f Cyberpunk2077.exe")
    assert not pattern.search("c:\\windows\\system32\\umu.exe /home/deck/ModOrganizer2-cyberpunk2077/ModOrganizer.exe")
    with patch.object(cyberpunk.background, "run_host", return_value=CompletedProcess([], 1)):
        assert not cyberpunk.game_running()


# --- syncing the mods -----------------------------------------------------------


def test_only_an_instance_handed_to_modsync_syncs(tmp_path):
    """One MO2-LINT installed is played, but never synced unless chosen."""
    from modsync.sync import folders

    found = make_instance(tmp_path)
    state_file = write_mo2lint_state(tmp_path, ("cyberpunk2077", found))
    with patch("modsync.mo2.installers.mo2lint.mo2lint_state_path", return_value=state_file):
        assert cyberpunk.find_instance() == found
        assert cyberpunk.MODS_FOLDER.path() is None
        cyberpunk.use_instance(found)
        assert cyberpunk.MODS_FOLDER.path() == found.resolve()
    (found / "ModOrganizer.exe").unlink()  # gone (an unmounted drive): nothing to sync
    assert cyberpunk.MODS_FOLDER.path() is None
    assert folders.spec_for("cp2077-mo2") is cyberpunk.MODS_FOLDER


def test_the_ignore_list_drops_play_leftovers_before_allowing_content():
    lines = [line for line in cyberpunk.MODS_FOLDER.ignore if line and not line.startswith("//")]
    first_allowed = lines.index("!/mods")
    assert set(cyberpunk.PLAY_LEFTOVERS) <= set(lines[:first_allowed])  # Syncthing: first match wins
    assert lines[-1] == "/*"
    assert {"!/profiles", "!/downloads", "!/overwrite", f"!/{cyberpunk.VAULT_RECORD}"} <= set(lines)


def test_an_offer_fills_only_an_instance_without_mods(tmp_path):
    root = make_instance(tmp_path)
    assert cyberpunk.MODS_FOLDER.accepts(root)  # a fresh install: no mods folder yet
    (root / "mods").mkdir()
    assert cyberpunk.MODS_FOLDER.accepts(root)
    (root / "mods/Cyber Engine Tweaks").mkdir()
    assert not cyberpunk.MODS_FOLDER.accepts(root)


def test_cets_databases_stay_out():
    lines = [line for line in cyberpunk.MODS_FOLDER.ignore if line and not line.startswith("//")]
    assert lines.index("*.sqlite3*") < lines.index("!/overwrite")


def test_a_record_owed_since_sharing_is_written_once_the_version_can_be_read(tmp_path):
    root = make_instance(tmp_path)
    with patch.object(cyberpunk, "game_version", return_value=None):
        cyberpunk.MODS_FOLDER.on_share(root)
        cyberpunk.MODS_FOLDER.tidy(root)
    assert not (root / cyberpunk.VAULT_RECORD).exists()
    with patch.object(cyberpunk, "game_version", return_value=("2.5", "3.0.88.46235")):
        cyberpunk.MODS_FOLDER.tidy(root)
    assert cyberpunk.vault_version(root) == ("2.5", "3.0.88.46235")
    assert "record_owed" not in json.loads(cyberpunk._settings_path().read_text())


def test_a_joining_machine_never_writes_the_record(tmp_path):
    root = make_instance(tmp_path)
    with patch.object(cyberpunk, "game_version", return_value=("2.5", "3.0.88.46235")):
        cyberpunk.MODS_FOLDER.tidy(root)  # took up an offer: no record owed here
    assert not (root / cyberpunk.VAULT_RECORD).exists()


def test_every_recorded_version_counts_conflict_copies_too(tmp_path):
    root = make_instance(tmp_path)
    (root / cyberpunk.VAULT_RECORD).write_text(json.dumps({"game_version": "2.5", "build": "3.0.88.46235"}))
    (root / "modsync-cp2077-vault.sync-conflict-20261108-101010-ABCDEFG.json").write_text(
        json.dumps({"game_version": "2.3", "build": "3.0.80.1"}))

    with patch.object(cyberpunk, "game_version", return_value=("2.5", "3.0.88.46235")):
        problem = cyberpunk.version_problem(instance=root)
    assert problem is not None and "set up for 2.3 (build 3.0.80.1)" in problem
    assert f"!/{cyberpunk.RECORD_CONFLICTS}" in cyberpunk.MODS_FOLDER.ignore  # and they reach the others


def test_the_sharing_machine_records_its_game_version_once(tmp_path):
    root = make_instance(tmp_path)
    with patch.object(cyberpunk, "game_version", return_value=("2.5", "3.0.88.46235")):
        cyberpunk.MODS_FOLDER.on_share(root)
    with patch.object(cyberpunk, "game_version", return_value=("2.6", "3.0.90.1")):
        cyberpunk.MODS_FOLDER.on_share(root)  # a later share doesn't rewrite it

    assert cyberpunk.vault_version(root) == ("2.5", "3.0.88.46235")


def test_another_game_version_here_is_only_reported(tmp_path):
    root = make_instance(tmp_path)
    (root / cyberpunk.VAULT_RECORD).write_text(json.dumps({"game_version": "2.5", "build": "3.0.88.46235"}))

    with patch.object(cyberpunk, "game_version", return_value=("2.5", "3.0.88.46235")):
        assert cyberpunk.version_problem(instance=root) is None
    with patch.object(cyberpunk, "game_version", return_value=("2.6", "3.0.90.1")):
        problem = cyberpunk.version_problem(instance=root)
    assert "here is 2.6 (build 3.0.90.1)" in problem and "set up for 2.5 (build 3.0.88.46235)" in problem
    with patch.object(cyberpunk, "game_version", return_value=None):
        assert cyberpunk.version_problem(instance=root) is None  # can't tell: say nothing


def test_game_version_reads_the_exe(tmp_path):
    from tests.test_gameversion import fake_pe

    game = make_game(tmp_path)
    (game.install_path / "bin/x64/Cyberpunk2077.exe").write_bytes(
        fake_pe(3, 0, 88, 46235, strings={"ProductVersion": "2.5"}))
    assert cyberpunk.game_version(game) == ("2.5", "3.0.88.46235")


# --- for the app ------------------------------------------------------------------


def test_overview_reads_the_prefix_and_how_far_the_setup_got(tmp_path):
    instance = make_instance(tmp_path)
    cyberpunk.use_instance(instance)
    for prefix_ready in (True, False):
        with patch.object(cyberpunk, "find_game", return_value=make_game(tmp_path / str(prefix_ready),
                                                                       prefix_ready=prefix_ready)), \
                patch.object(cyberpunk, "hook_status", return_value=None):
            ov = cyberpunk.overview()
        assert ov.prefix_ready is prefix_ready
        assert ov.needs_setup and ov.chosen
    (instance / "plugins/rootbuilder").mkdir(parents=True)
    (instance / "plugins/rootbuilder/__init__.py").write_text("")
    with patch.object(cyberpunk, "find_game", return_value=None):
        assert not cyberpunk.overview().needs_setup


class TestUseAnotherInstance:
    @pytest.fixture
    def service(self):
        from modsync.service import ModSyncService

        svc = ModSyncService(manager=object())
        with patch.object(svc, "ensure_running"), patch.object(cyberpunk, "refresh_hook", return_value=[]):
            yield svc

    def test_mods_sync_moves_along_before_it_returns(self, tmp_path, service):
        from modsync.state import State

        new = make_instance(tmp_path)
        State.update(game_folders={"cp2077-mo2": "/old/instance"})

        def moved(*, force):
            assert force
            State.update(game_folders={"cp2077-mo2": str(new.resolve())})

        with patch.object(cyberpunk, "game_running", return_value=False), \
                patch.object(service, "_refresh_game_folders", side_effect=moved) as refresh:
            notes = service.use_cyberpunk_instance(new)
        refresh.assert_called_once()
        assert notes == [f"Using Mod Organizer 2 in {new.resolve()}."]

    def test_a_move_still_pending_is_said(self, tmp_path, service):
        from modsync.state import State

        State.update(game_folders={"cp2077-mo2": "/old/instance"})
        with patch.object(cyberpunk, "game_running", return_value=False), \
                patch.object(service, "_refresh_game_folders"):
            notes = service.use_cyberpunk_instance(make_instance(tmp_path))
        assert "wait a moment before playing" in notes[-1]

    def test_refused_while_the_game_runs_with_sync_on(self, tmp_path, service):
        from modsync.state import State

        State.update(game_folders={"cp2077-mo2": "/old/instance"})
        with patch.object(cyberpunk, "game_running", return_value=True), \
                pytest.raises(RuntimeError, match="Close Cyberpunk 2077"):
            service.use_cyberpunk_instance(make_instance(tmp_path))
        assert cyberpunk.chosen_instance() is None

    def test_without_sync_nothing_needs_to_move(self, tmp_path, service):
        from modsync.state import State

        State.update(game_folders={})
        with patch.object(cyberpunk, "game_running", return_value=True), \
                patch.object(service, "_refresh_game_folders") as refresh:
            service.use_cyberpunk_instance(make_instance(tmp_path))
        refresh.assert_not_called()


# --- Nexus downloads --------------------------------------------------------------

LINK = "nxm://cyberpunk2077/mods/2987/files/168466?key=k&expires=1&user_id=2"


def with_drives(tmp_path: Path, game: library.GogGame) -> None:
    """The prefix's drive links as umu leaves them: X: is the home folder."""
    drives = game.drive_c.parent / "dosdevices"
    drives.mkdir()
    (drives / "c:").symlink_to("../drive_c")
    (drives / "x:").symlink_to(tmp_path)
    (drives / "z:").symlink_to("/")


def pgrep(*lines: str):
    from subprocess import CompletedProcess

    out = "".join(f"{1000 + i} {line}\n" for i, line in enumerate(lines))
    return patch.object(cyberpunk.background, "run_host", return_value=CompletedProcess([], 0 if lines else 1, out, ""))


@pytest.mark.parametrize("line, running", [
    (r"X:\ModOrganizer2-Cyberpunk2077\ModOrganizer.exe -p Default", True),
    (r"x:\modorganizer2-cyberpunk2077\modorganizer.exe", True),  # Windows paths ignore case
    (r"Z:{tmp}\ModOrganizer2-Cyberpunk2077\ModOrganizer.exe run -e Cyberpunk 2077", True),
    (r"X:\ModOrganizer2-SkyrimSE\ModOrganizer.exe -p Default", False),  # another instance
    (r"Q:\ModOrganizer2-Cyberpunk2077\ModOrganizer.exe", False),  # no such drive in this prefix
])
def test_mo2_running_follows_the_prefixs_drive_letters(tmp_path, line, running):  # environ unreadable: path decides
    game, instance = make_game(tmp_path), make_instance(tmp_path)
    with_drives(tmp_path, game)
    with pgrep(line.replace("{tmp}", str(tmp_path).replace("/", "\\"))):
        assert cyberpunk.mo2_running(game, instance) is running


class Downloading:
    """cyberpunk.download with the game, instance and every way out stubbed."""

    def __init__(self, tmp_path, *, running=False, hook_on=False):
        self.game, self.instance = make_game(tmp_path), make_instance(tmp_path)
        self.popen = patch.object(cyberpunk.subprocess, "Popen")
        self.patches = [
            patch.object(cyberpunk, "find_game", return_value=self.game),
            patch.object(cyberpunk, "find_instance", return_value=self.instance),
            patch.object(cyberpunk, "mo2_running", return_value=running),
            patch.object(cyberpunk, "_host_which", return_value="/usr/bin/umu-run"),
            patch.object(cyberpunk, "refresh_hook", return_value=[]),
            patch.object(cyberpunk.heroic_hook, "status",
                         return_value=cyberpunk.heroic_hook.HookStatus(hook_on, hook_on, tmp_path / "s.sh")),
            patch.object(cyberpunk.heroic_hook, "leave_download"),
            patch.object(cyberpunk.heroic_hook, "launch"),
            patch("modsync.mo2.launch.Launcher.start", return_value="Starting Mod Organizer 2…"),
        ]

    def __enter__(self):
        cyberpunk._launch_record().unlink(missing_ok=True)
        self.mocks = [p.start() for p in self.patches]
        self.popen_mock = self.popen.start()
        self.popen_mock.return_value.wait.return_value = 0
        return self

    def __exit__(self, *exc):
        cyberpunk._launch_record().unlink(missing_ok=True)
        self.popen.stop()
        for p in reversed(self.patches):
            p.stop()


def test_an_open_mo2_gets_the_link_from_a_second_mo2_that_doesnt_wait(tmp_path):
    with Downloading(tmp_path, running=True) as d:
        assert cyberpunk.download(LINK) == "Passed the download to Mod Organizer 2."
        argv = d.popen_mock.call_args.args[0]
        env = d.popen_mock.call_args.kwargs["env"]
    assert argv == ["/usr/bin/umu-run", str(d.instance / "ModOrganizer.exe"), LINK]
    assert env["PROTON_VERB"] == "runinprefix" and env["WINEPREFIX"] == str(d.game.prefix)


def test_a_closed_mo2_opens_through_heroic_while_its_hook_is_on(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        assert "through Heroic" in cyberpunk.download(LINK)
        leave, launch = cyberpunk.heroic_hook.leave_download, cyberpunk.heroic_hook.launch
        assert leave.call_args.args == (d.game, LINK)
        assert launch.call_args.args == (d.game,)
        assert not d.popen_mock.called


def test_without_the_hook_mo2_opens_as_cyberpunk_open_does_with_the_link(tmp_path):
    from modsync.mo2.launch import Launcher

    with Downloading(tmp_path) as d:
        assert cyberpunk.download(LINK) == "Starting Mod Organizer 2…"
        (plan,) = Launcher.start.call_args.args
    assert plan.command == [str(d.instance / "ModOrganizer.exe"), "-p", "Default", LINK]
    assert not plan.play and plan.env["WINEDLLOVERRIDES"].endswith("winmm,version=n,b")


def test_downloads_are_modsyncs_only_with_the_game_and_an_instance(tmp_path):
    instance = make_instance(tmp_path)
    with patch.object(cyberpunk, "find_game", return_value=make_game(tmp_path)), \
            patch.object(cyberpunk, "find_instance", return_value=instance):
        assert cyberpunk.takes_downloads()
    with patch.object(cyberpunk, "find_game", return_value=None), \
            patch.object(cyberpunk, "find_instance", return_value=instance):
        assert not cyberpunk.takes_downloads()


def test_an_mo2_with_the_same_path_in_another_prefix_isnt_this_one(tmp_path):
    from subprocess import CompletedProcess

    game, instance = make_game(tmp_path), make_instance(tmp_path)
    with_drives(tmp_path, game)
    line = "1000 X:\\ModOrganizer2-Cyberpunk2077\\ModOrganizer.exe\n"

    def host(prefix):
        def run_host(cmd, **_kw):
            if cmd[0] == "cat":
                return CompletedProcess(cmd, 0, f"HOME=/h\0WINEPREFIX={prefix}/\0", "")
            return CompletedProcess(cmd, 0, line, "")
        return patch.object(cyberpunk.background, "run_host", side_effect=run_host)

    with host(tmp_path / "elsewhere"):
        assert not cyberpunk.mo2_running(game, instance)
    with host(game.drive_c.parent):
        assert cyberpunk.mo2_running(game, instance)


def test_a_link_while_mo2_is_starting_waits_for_it_instead_of_launching_again(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        assert "through Heroic" in cyberpunk.download(LINK)
        cyberpunk.heroic_hook.launch_note(d.game).parent.mkdir(parents=True, exist_ok=True)
        cyberpunk.heroic_hook.launch_note(d.game).write_text(LINK)  # the hook hasn't run yet
        cyberpunk.mo2_running.side_effect = [False, False, True, True]
        assert cyberpunk.download(LINK + "2", poll=0) == "Passed the download to Mod Organizer 2."
        assert cyberpunk.heroic_hook.launch.call_count == 1
        assert cyberpunk.heroic_hook.leave_download.call_count == 1
        assert d.popen_mock.call_args.args[0][-1] == LINK + "2"


def no_waiting():
    return patch("time.sleep", side_effect=AssertionError("waited"))


def taken_ago(game, seconds: float) -> None:
    """The hook took the link ``seconds`` ago."""
    import os
    import time

    taken = cyberpunk.heroic_hook.launch_taken(game)
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_text("")
    os.utime(taken, (time.time() - seconds,) * 2)


def test_an_mo2_that_never_showed_up_is_opened_again_at_once(tmp_path):
    """The wait runs from when the hook took the link, for every link alike:
    a link clicked well after that doesn't wait out a wait of its own."""
    import os
    import time

    with Downloading(tmp_path, hook_on=True) as d, no_waiting():
        cyberpunk.download(LINK)
        record = cyberpunk._launch_record()
        os.utime(record, (time.time() - 100,) * 2)
        taken_ago(d.game, 99)  # and MO2 never came
        assert "through Heroic" in cyberpunk.download(LINK)
        assert cyberpunk.heroic_hook.launch.call_count == 2


def test_a_link_just_taken_by_the_hook_waits_for_mo2(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        cyberpunk.download(LINK)
        taken_ago(d.game, 0)
        cyberpunk.mo2_running.side_effect = [False, True, True]
        assert cyberpunk.download(LINK + "2", poll=0) == "Passed the download to Mod Organizer 2."
        assert cyberpunk.heroic_hook.launch.call_count == 1


def test_a_failed_launch_leaves_nothing_to_wait_for(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        cyberpunk.heroic_hook.leave_download.side_effect = lambda game, url: (
            cyberpunk.heroic_hook.launch_note(game).parent.mkdir(parents=True, exist_ok=True),
            cyberpunk.heroic_hook.launch_note(game).write_text(url))
        cyberpunk.heroic_hook.launch.side_effect = cyberpunk.heroic_hook.HookError("no Heroic")
        with pytest.raises(cyberpunk.heroic_hook.HookError):
            cyberpunk.download(LINK)
        assert not cyberpunk.heroic_hook.launch_note(d.game).exists()
        cyberpunk.heroic_hook.launch.side_effect = None
        with no_waiting():
            assert "through Heroic" in cyberpunk.download(LINK)


def test_a_direct_launch_gets_its_own_short_wait(tmp_path):
    from modsync.mo2.launch import Launcher

    with Downloading(tmp_path):
        cyberpunk.download(LINK)
        cyberpunk.mo2_running.side_effect = [False, True, True]
        assert cyberpunk.download(LINK + "2", poll=0) == "Passed the download to Mod Organizer 2."
        assert Launcher.start.call_count == 1
