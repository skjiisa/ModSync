"""A game from the table (modsync.mo2games) that isn't Cyberpunk: Starfield
from Steam, The Witcher 3 from GOG and Dragon Age 2 from Epic through Heroic,
each on a throwaway Steam or Heroic. Nobody owns them here; nothing they need
is theirs alone."""

import io
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modsync import cli, launchhook, mo2games, nxm
from modsync.heroic import hook as heroic_hook
from modsync.heroic import library
from modsync.mo2 import downloads, steam_downloads
from modsync.mo2games import heroic as heroic_store
from modsync.mo2games import steam as steam_store
from modsync.steam import libraries as steam_libraries
from modsync.steam import shortcuts, vdf
from tests import fakesteam
from tests.test_heroic_hook import RECORDER, executable
from tests.test_heroic_library import make_heroic

STARFIELD = 1716740
WITCHER_GOG = "1207664643"
DRAGONAGE2_EPIC = "4e8c0f56bcf948d581e30c58283d183c"
PROTON = {"bin": "/opt/proton/proton", "name": "proton-x", "type": "proton"}
LINK = "nxm://starfield/mods/1/files/2?key=k&expires=1&user_id=3"


def starfield():
    return mo2games.get("starfield")


def add_steam_game(root: Path, appid: int, installdir: str, *, prefix: bool = True) -> Path:
    """One more game in the fake Steam library, with its Proton prefix."""
    steamapps = root / "steamapps"
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        vdf.dumps({"AppState": {"appid": str(appid), "name": installdir, "installdir": installdir,
                                "buildid": "23518663"}}) + "\n")
    folders = vdf.load(steamapps / "libraryfolders.vdf")
    folders["libraryfolders"]["0"]["apps"][str(appid)] = "1"
    (steamapps / "libraryfolders.vdf").write_text(vdf.dumps(folders) + "\n")
    game_dir = steamapps / "common" / installdir
    game_dir.mkdir(parents=True)
    if prefix:
        compat = steamapps / "compatdata" / str(appid)
        (compat / "pfx" / "drive_c").mkdir(parents=True)
        (compat / "config_info").write_text(f"version\n{steamapps}/common/Proton - Experimental/files/lib/\n")
    return game_dir


def make_instance(tmp_path: Path, mo2_name: str, ini: str = "", *, name: str = "MO2") -> Path:
    instance = tmp_path / name
    (instance / "mods").mkdir(parents=True)
    (instance / "ModOrganizer.exe").write_bytes(b"MZ")
    (instance / "ModOrganizer.ini").write_text(
        f"[General]\ngameName={mo2_name}\nselected_profile=@ByteArray(Default)\n{ini}")
    return instance


def programs(*entries: tuple[str, str]) -> str:
    """MO2's [customExecutables], as MO2 writes it."""
    lines = ["", "[customExecutables]", f"size={len(entries)}"]
    for i, (title, binary) in enumerate(entries, 1):
        lines += [f"{i}\\arguments=", f"{i}\\binary=Z:{binary}", f"{i}\\title={title}"]
    return "\n".join(lines) + "\n"


@pytest.fixture
def machine(tmp_path):
    """A machine with Starfield in Steam, and The Witcher 3 (GOG) and Dragon
    Age 2 (Epic) in Heroic. Nothing of the real one is read."""
    root = fakesteam.make_steam(tmp_path / "Steam")
    game_dir = add_steam_game(root, STARFIELD, "Starfield")
    (game_dir / "Starfield.exe").write_bytes(b"MZ")
    witcher = tmp_path / "Games/The Witcher 3"
    dragon = tmp_path / "Games/DragonAge2"
    for folder in (witcher, dragon):
        folder.mkdir(parents=True)
    heroic = make_heroic(tmp_path / "heroic", installed=[
        {"platform": "windows", "install_path": str(witcher), "is_dlc": False, "version": "4.04",
         "appName": WITCHER_GOG, "buildId": "1"},
    ], games_config={
        WITCHER_GOG: {"winePrefix": str(tmp_path / "prefix-w3"), "wineVersion": PROTON},
        DRAGONAGE2_EPIC: {"winePrefix": str(tmp_path / "prefix-da2"), "wineVersion": PROTON},
    })
    (heroic / "legendaryConfig/legendary").mkdir(parents=True)
    (heroic / "legendaryConfig/legendary/installed.json").write_text(json.dumps({
        DRAGONAGE2_EPIC: {"app_name": DRAGONAGE2_EPIC, "install_path": str(dragon), "is_dlc": False,
                          "platform": "Windows", "version": "1.04", "title": "Dragon Age II"},
        "SomeDlc": {"app_name": "SomeDlc", "install_path": str(dragon), "is_dlc": True, "platform": "Windows"},
    }))
    for prefix in ("prefix-w3", "prefix-da2"):
        (tmp_path / prefix / "drive_c").mkdir(parents=True)
    plat = SimpleNamespace(name="linux", steam_roots=lambda: [root], heroic_config_dirs=lambda: [heroic])
    state_file = tmp_path / "mo2-lint-state.json"
    keys = ("starfield", "witcher3", "dragonage2", "falloutnv")
    patches = [
        patch("modsync.platforms.current", return_value=plat),
        # The suite hides this machine's Steam from the launch hook; this one is the fake.
        patch.object(launchhook, "steam_env", lambda: launchhook.SteamEnv(root, steam_libraries.all_libraries([root]))),
        patch("modsync.mo2.installers.mo2lint.mo2lint_state_path", return_value=state_file),
        patch.object(shortcuts, "steam_is_running", return_value=True),
        patch("modsync.background.in_flatpak", return_value=False),
        patch.object(downloads, "shared_dir", return_value=tmp_path / "launches"),
        patch.object(mo2games, "host_which", return_value="/usr/bin/umu-run"),
        patch("modsync.nxm.claim_links", return_value=None),
        patch.dict(os.environ, {"XDG_CONFIG_HOME": str(tmp_path / "config"), "DISPLAY": ":0"}),
    ]
    for p in patches:
        p.start()
    os.environ.pop("STEAM_GAME_DISPLAY_0", None)
    try:
        yield SimpleNamespace(tmp=tmp_path, root=root, heroic=heroic, game_dir=game_dir, witcher=witcher,
                              dragon=dragon, state_file=state_file, keys=keys)
    finally:
        for p in reversed(patches):
            p.stop()


def test_the_suite_never_sees_this_machines_stores():
    """Without the fixture above, a developer's Steam, Heroic and MO2-LINT
    installs stay out of every test (tests/conftest.py), whatever they hold."""
    assert mo2games.here() == []
    assert all(game.find_game() is None and game.find_instance() is None for game in mo2games.all_games().values())
    assert mo2games.refresh_hooks() == []


# --- the game in its store --------------------------------------------------------


def test_a_steam_game_is_found_with_its_prefix(machine):
    game = starfield().find_game()

    assert game.store == "steam" and game.install_path == machine.game_dir
    assert game.version == "23518663" and game.app_name == str(STARFIELD)
    assert game.drive_c == machine.root / "steamapps/compatdata" / str(STARFIELD) / "pfx/drive_c"
    assert steam_store.not_ready(starfield(), game) is None


def test_a_prefix_steam_hasnt_made_asks_for_one_launch(machine):
    shutil.rmtree(machine.root / "steamapps/compatdata" / str(STARFIELD))
    instance = make_instance(machine.tmp, "Starfield")

    with pytest.raises(mo2games.GameError, match="Launch the game once from Steam"):
        starfield().build_plan(instance, starfield().find_game())
    assert not starfield().overview(hook=False).prefix_ready


def test_heroics_stores_are_found_by_their_ids(machine):
    witcher = mo2games.get("witcher3").find_game()
    dragon = mo2games.get("dragonage2").find_game()

    assert (witcher.store, witcher.runner, witcher.install_path) == ("gog", "gog", machine.witcher)
    assert (dragon.store, dragon.runner, dragon.install_path) == ("epic", "legendary", machine.dragon)
    assert dragon.version == "1.04" and not dragon.pinned
    assert [g.app_name for g in library.installed([machine.heroic])] == [WITCHER_GOG, DRAGONAGE2_EPIC]  # no DLC
    assert library.find_game("epic", "SomeDlc", [machine.heroic]) is None


def test_the_games_here_are_the_installed_ones_and_the_ones_with_an_instance(machine):
    assert {g.key for g in mo2games.here()} == {"starfield", "witcher3", "dragonage2"}
    mo2games.get("falloutnv").use_instance(make_instance(machine.tmp, "New Vegas"))  # its drive is unplugged, say
    assert "falloutnv" in {g.key for g in mo2games.here()}
    assert mo2games.get("falloutnv").find_game() is None


def test_a_game_in_two_stores_is_the_one_its_instance_was_set_up_for(machine):
    """The Witcher 3 in Steam too: the first store wins until an instance says otherwise."""
    add_steam_game(machine.root, 292030, "The Witcher 3")
    witcher = mo2games.get("witcher3")
    assert [g.store for g in witcher.installs()] == ["steam", "gog"]
    assert witcher.find_game().store == "steam"

    witcher.use_instance(make_instance(machine.tmp, "The Witcher 3: Wild Hunt", "game_edition=GOG\n"))
    assert witcher.find_game().store == "gog"
    assert witcher.steam_instance() is None  # Steam's launches and links aren't this instance's


def test_each_game_keeps_its_own_settings_and_folder(machine):
    game = starfield()
    instance = game.use_instance(make_instance(machine.tmp, "Starfield"))

    assert json.loads((machine.tmp / "config/modsync/games/starfield.json").read_text()) == {"instance": str(instance)}
    assert game.folder.key == "starfield-mo2" and game.folder.folder_id == "modsync-starfield-mo2"
    assert game.folder.path() == instance
    assert "Syncs Starfield's Mod Organizer 2 content" in game.folder.stignore_text()
    assert game.folder.ignore[-1] == "/*" and "!/mods" in game.folder.ignore
    assert mo2games.for_folder("starfield-mo2") is game and mo2games.for_steam(STARFIELD) is game
    assert game.ui_key == "mo2-starfield" and mo2games.for_ui("mo2-starfield") is game


def test_an_instance_for_another_game_is_refused(machine):
    other = make_instance(machine.tmp, "Skyrim Special Edition")

    with pytest.raises(mo2games.GameError, match="manages Skyrim Special Edition, not Starfield"):
        starfield().use_instance(other)


# --- which of MO2's programs plays the game ---------------------------------------


def test_play_runs_the_first_program_mo2_lists_from_the_games_folder(machine):
    """MO2 lists a script extender ahead of the game once it's there, and its
    own tools after: MO2's order decides, and the player can change it in MO2."""
    game = starfield()
    (machine.game_dir / "sfse_loader.exe").write_bytes(b"MZ")
    listed = programs(("Explore Virtual Folder", f"{machine.tmp}/MO2/explorer++/Explorer++.exe"),
                      ("SFSE", f"{machine.game_dir}/sfse_loader.exe"),
                      ("Starfield", f"{machine.game_dir}/Starfield.exe"))
    instance = make_instance(machine.tmp, "Starfield", listed)

    plan = game.build_plan(instance, game.find_game(), play=True)

    assert plan.command == [str(instance / "ModOrganizer.exe"), "-p", "Default", "run", "-e", "SFSE"]
    assert plan.target == "SFSE" and plan.play


def test_a_program_that_is_gone_is_passed_over(machine):
    """A script extender taken out again leaves its entry in MO2."""
    game = starfield()
    listed = programs(("SFSE", f"{machine.game_dir}/sfse_loader.exe"),
                      ("Starfield", f"{machine.game_dir}/Starfield.exe"))
    instance = make_instance(machine.tmp, "Starfield", listed)

    assert game.build_plan(instance, game.find_game(), play=True).command[-3:] == ["run", "-e", "Starfield"]


def test_before_mo2_lists_its_programs_the_games_own_runs_through_mo2(machine):
    game = starfield()
    instance = make_instance(machine.tmp, "Starfield")

    plan = game.build_plan(instance, game.find_game(), play=True)

    exe = machine.game_dir / "Starfield.exe"
    assert plan.command[-4:] == ["run", "-c", f"Z:{machine.game_dir}", f"Z:{exe}"]
    exe.unlink()
    with pytest.raises(mo2games.GameError, match="is missing. Check the game's files in Steam"):
        game.build_plan(instance, game.find_game(), play=True)


# --- Steam ---------------------------------------------------------------------------


def test_mo2_runs_in_the_games_own_steam_prefix(machine):
    game = starfield()
    instance = make_instance(machine.tmp, "Starfield")
    common = machine.root / "steamapps/common"

    with patch("modsync.mo2.launch.launchhook.game_proton", return_value=None):
        plan = game.build_plan(instance, game.find_game())

    assert plan.argv == [str(common / "SteamLinuxRuntime_4/_v2-entry-point"), "--verb=run", "--",
                         str(common / "Proton - Experimental/proton"), "run",
                         str(instance / "ModOrganizer.exe"), "-p", "Default"]
    assert plan.env["STEAM_COMPAT_DATA_PATH"] == str(machine.root / "steamapps/compatdata" / str(STARFIELD))
    assert plan.env["SteamAppId"] == plan.env["STEAM_COMPAT_APP_ID"] == str(STARFIELD)
    assert "WINEDLLOVERRIDES" not in plan.env and plan.hook_env == {}


def test_steams_play_button_goes_through_modsyncs_hook(machine):
    game = starfield()
    game.use_instance(make_instance(machine.tmp, "Starfield"))
    tool = machine.root / "compatibilitytools.d" / f"modsync_{STARFIELD}_proton"

    with patch.object(shortcuts, "steam_is_running", return_value=False), \
            patch.object(launchhook, "modsync_command", return_value=["/usr/bin/modsync"]):
        said = game.enable_hook()
        assert "Launch hook enabled" in said[0] and "Starfield" in said[0]
        assert (tool / "proton").is_file() and f'appid="{STARFIELD}"' in (tool / "proton").read_text()
        assert game.hook_status().enabled
        assert STARFIELD in launchhook.hooked_appids()
        assert "Launch hook off" in game.disable_hook()
    assert not tool.exists() and not game.hook_status().enabled


def test_play_and_open_go_through_steam_while_its_hook_is_on(machine):
    """Steam's launch, so Steam Cloud syncs the saves: the note tells the hub
    whether to play or open MO2."""
    from modsync.mo2.launch import Launcher

    game = starfield()
    game.use_instance(make_instance(machine.tmp, "Starfield"))
    on = SimpleNamespace(installed=True, selected=True, steam_running=True)
    asked = []

    def run_host(argv, **_kw):
        asked.append(argv)
        return CompletedProcess(argv, 1 if argv[0] == "pgrep" else 0, "", "")

    with patch.object(launchhook, "status", return_value=on), patch.object(launchhook, "upgrade", return_value=None), \
            patch("modsync.background.run_host", side_effect=run_host), \
            patch("modsync.mo2.launch.launchhook.game_proton", return_value=None), \
            patch.object(Launcher, "start") as start:
        assert game.launch(Launcher(), play=True) == "Starting Starfield through Steam…"
        assert steam_downloads.note_path(game.game).read_text().strip() == downloads.PLAY
        assert ["xdg-open", f"steam://rungameid/{STARFIELD}"] in asked
        with pytest.raises(mo2games.GameError, match="already open for Starfield"):
            game.launch(Launcher(), play=False)
        start.assert_not_called()
    assert steam_downloads.take_request(game.game) == downloads.PLAY
    assert steam_downloads.take_request(game.game) is None  # taken once


def test_without_the_hook_modsync_starts_mo2_itself(machine):
    from modsync.mo2.launch import Launcher

    game = starfield()
    instance = game.use_instance(make_instance(machine.tmp, "Starfield"))

    with patch("modsync.mo2.launch.launchhook.game_proton", return_value=None), \
            patch("modsync.mo2.vcruntime.ensure_instance_runtime", return_value=[]) as runtime, \
            patch.object(Launcher, "start", return_value="Starting Mod Organizer 2…") as start:
        assert game.launch(Launcher(), play=False) == "Starting Mod Organizer 2…"

    (plan,) = start.call_args.args
    assert plan.cwd == instance and not plan.play
    runtime.assert_called_once()  # as Open MO2 does for Skyrim
    assert (instance / "portable.txt").exists()


class Hub:
    """'modsync launch hub' for Starfield: what it hands back to Steam's hook."""

    def __init__(self):
        self.out = io.BytesIO()
        self.notes: list[str] = []

    def run(self) -> int:
        with patch("modsync.mo2.launch.launchhook.game_proton", return_value=None), \
                patch("modsync.mo2.vcruntime.ensure_instance_runtime", return_value=[]), \
                patch("modsync.logging_setup.configure"), patch.object(nxm, "notify", self.notes.append):
            return cli._managed_hub(starfield(), self.out)

    @property
    def fields(self) -> list[str]:
        return [f.decode() for f in self.out.getvalue().split(b"\0")[:-1]]


def test_steams_play_plays_through_mo2_without_a_window(machine):
    instance = starfield().use_instance(make_instance(
        machine.tmp, "Starfield", programs(("Starfield", f"{machine.game_dir}/Starfield.exe"))))
    hub = Hub()

    assert hub.run() == launchhook.EXIT_CONTINUE

    assert hub.fields == [launchhook.HANDOFF_TAG, str(machine.root / "steamapps/common/Proton - Experimental"),
                          str(instance), str(instance / "ModOrganizer.exe"), "-p", "Default",
                          "run", "-e", "Starfield"]
    assert hub.notes == []


@pytest.mark.parametrize("note, tail", [
    (downloads.OPEN, ["-p", "Default"]),
    (downloads.PLAY, ["-p", "Default", "run", "-c"]),
    (LINK, ["-p", "Default", LINK]),
])
def test_a_note_modsync_left_says_what_the_launch_is_for(machine, note, tail):
    game = starfield()
    instance = game.use_instance(make_instance(machine.tmp, "Starfield"))
    path = steam_downloads.note_path(game.game)
    path.parent.mkdir(parents=True)
    path.write_text(note + "\n")
    hub = Hub()

    assert hub.run() == launchhook.EXIT_CONTINUE
    assert hub.fields[3:3 + 1 + len(tail)] == [str(instance / "ModOrganizer.exe"), *tail]
    assert not path.exists()


def test_without_an_instance_steams_launch_goes_on_as_it_was(machine):
    hub = Hub()

    assert hub.run() == launchhook.EXIT_CONTINUE
    assert hub.out.getvalue() == b"" and hub.notes == []


def test_a_launch_mo2_cant_take_starts_the_game_and_says_why(machine):
    """ModSync never keeps a game from starting."""
    starfield().use_instance(make_instance(machine.tmp, "Starfield"))
    shutil.rmtree(machine.root / "steamapps/compatdata" / str(STARFIELD))
    hub = Hub()

    assert hub.run() == launchhook.EXIT_CONTINUE
    assert hub.out.getvalue() == b""
    assert len(hub.notes) == 1 and "Starfield is starting without Mod Organizer 2" in hub.notes[0]
    assert "Launch the game once from Steam" in hub.notes[0]


def test_the_hub_answers_for_a_table_game_before_skyrims_window(machine):
    with patch.object(cli, "_managed_hub", return_value=0) as managed, \
            patch.object(cli, "_open_for_download") as skyrims:
        assert cli._launch_hub(["--appid", str(STARFIELD), "--through", "proton_experimental"]) == 0
    assert managed.call_args.args[0] is starfield()
    skyrims.assert_not_called()


def test_a_steam_games_nexus_links_reach_its_mo2(machine):
    game = starfield()
    assert not game.takes_downloads()
    instance = game.use_instance(make_instance(machine.tmp, "Starfield"))
    assert game.takes_downloads() and steam_downloads.instance(game.game) == instance

    with patch.object(steam_downloads, "download", return_value="Passed the download to Mod Organizer 2.") as down:
        assert nxm.handle(LINK) == "Passed the download to Mod Organizer 2."
    assert down.call_args.args == (game.game, LINK)

    with patch("modsync.mo2.launch.launchhook.game_proton", return_value=None):
        plan = steam_downloads._plan(game.game, LINK, prepare=False)
    assert plan.command[-1] == LINK and plan.cwd == instance


def test_editions_share_nexus_name_and_the_one_set_up_takes_the_link(machine):
    assert [g.key for g in mo2games.for_nexus("skyrimspecialedition")] == ["skyrim_vr"]
    assert len(nxm.HANDLERS["skyrimspecialedition"]) == 2  # Skyrim Special Edition's own, then Skyrim VR's
    assert [g.key for g in mo2games.for_nexus("fallout3")] == ["fallout3", "fallout3_goty"]


def test_installing_for_steam_needs_steam_closed_then_runs_mo2lint_for_steam(machine):
    game = starfield()
    calls = []

    class Backend:
        def available(self):
            return True, ""

        def install(self, game, dest, **kw):
            calls.append((game.mo2lint_key, dest, kw))
            make_instance(dest.parent, "Starfield", name=dest.name)
            return SimpleNamespace(success=True, instance_path=dest, message="")

    dest = machine.tmp / "ModOrganizer2-starfield"
    with patch.object(game, "game_running", return_value=False):
        with pytest.raises(mo2games.GameError, match="Close Steam first"):
            game.install_mo2(dest, backend=Backend())
        assert calls == []
        with patch.object(shortcuts, "steam_is_running", return_value=False):
            assert game.install_mo2(dest, backend=Backend()) == dest
    assert calls == [("starfield", dest, {"launcher": "steam", "on_output": None})]  # MO2-LINT readies the prefix itself
    assert game.chosen_instance() == dest


# --- Heroic --------------------------------------------------------------------------


def test_a_gog_game_runs_mo2_with_umu_and_no_dll_overrides(machine):
    game = mo2games.get("witcher3")
    found = game.find_game()
    instance = make_instance(machine.tmp, "The Witcher 3: Wild Hunt")

    plan = game.build_plan(instance, found)

    assert plan.argv == ["/usr/bin/umu-run", str(instance / "ModOrganizer.exe"), "-p", "Default"]
    assert plan.env["GAMEID"] == "umu-292030" and plan.env["STORE"] == "gog"
    assert plan.env["WINEPREFIX"] == str(machine.tmp / "prefix-w3")
    assert "WINEDLLOVERRIDES" not in plan.env  # only games whose mod loaders need one get one


def test_an_epic_game_is_launched_by_heroics_legendary(machine):
    game = mo2games.get("dragonage2")
    found = game.find_game()
    instance = make_instance(machine.tmp, "Dragon Age 2")

    assert game.build_plan(instance, found).env["STORE"] == "egs"
    with patch.object(heroic_hook.subprocess, "Popen") as popen:
        heroic_hook.launch(found)
    assert popen.call_args.args[0] == ["xdg-open", f"heroic://launch?appName={DRAGONAGE2_EPIC}&runner=legendary"]


def test_installing_for_heroic_holds_mo2lints_prefix_steps_and_does_them_itself(machine):
    game = mo2games.get("dragonage")  # GOG id 1949616134, with tricks of its own
    folder = machine.tmp / "Games/DAO"
    folder.mkdir()
    found = library.HeroicGame("1949616134", folder, "1.05", "1", False, machine.heroic,
                               prefix=machine.tmp / "prefix-w3", wine=library.WineVersion(Path("/p/proton"), "p", "proton"))
    calls = []

    class Backend:
        def available(self):
            return True, ""

        def install(self, game, dest, **kw):
            calls.append(kw)
            make_instance(dest.parent, "Dragon Age: Origins", name=dest.name)
            return SimpleNamespace(success=True, instance_path=dest, message="")

    ran = []

    def run_in_prefix(mg, game, args, **_kw):
        ran.append(args)
        return CompletedProcess(args, 0, "win10\n" if args[-1] == "list-installed" else "", "")

    dest = machine.tmp / "ModOrganizer2-dragonage"
    with patch.object(game, "find_game", return_value=found), patch.object(game, "game_running", return_value=False), \
            patch.object(heroic_store, "run_in_prefix", run_in_prefix), \
            patch("modsync.mo2.vcruntime.prefix_runtime_problems", return_value=[]):
        game.install_mo2(dest, backend=Backend())

    assert calls == [{"launcher": "gog", "env": heroic_store.MO2LINT_ENV, "on_output": None}]
    assert ran == [["winetricks", "-q", "list-installed"],
                   ["winetricks", "-q", "arial", "fontsmooth=rgb", "xaudio2_7=native", "d3dcompiler_43", "d3dx9"]]
    assert game.setup_finished(dest)  # nothing more to add for a game without extras


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_heroics_play_hook_plays_the_first_program_from_the_games_folder(machine):
    """The same rule as ModSync's own Play, in the script Heroic runs."""
    game = mo2games.get("witcher3")
    found = game.find_game()
    (machine.witcher / "bin/x64_dx12").mkdir(parents=True)
    for program in ("bin/x64_dx12/witcher3.exe", "REDprelauncher.exe"):
        (machine.witcher / program).write_bytes(b"MZ")
    listed = programs(("Explore Virtual Folder", f"{machine.tmp}/MO2/explorer++/Explorer++.exe"),
                      ("The Witcher 3 (DX11)", f"{machine.witcher}/bin/x64/witcher3.exe"),  # not installed
                      ("The Witcher 3", f"{machine.witcher}/bin/x64_dx12/witcher3.exe"),
                      ("REDprelauncher", f"{machine.witcher}/REDprelauncher.exe"))
    instance = make_instance(machine.tmp, "The Witcher 3: Wild Hunt", listed)
    text = heroic_store.hook_script(game, found, instance)
    assert "game_exe=''" in text and "overridden=()" in text and "@@" not in text
    assert "modsync games witcher3 hook disable" in text and f"GOG {WITCHER_GOG}" in text
    script = executable(machine.tmp / "play.sh", text)
    umu = executable(machine.tmp / "bin/umu-run", RECORDER)
    record = machine.tmp / "record.jsonl"

    result = subprocess.run(
        ["bash", str(script), heroic_hook.MARKER, WITCHER_GOG, str(umu), str(machine.witcher / "REDprelauncher.exe")],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "RECORD": str(record), "WINEDLLOVERRIDES": "dxgi=n"})

    assert result.returncode == 0, result.stderr
    (call,) = [json.loads(line) for line in record.read_text().splitlines()]
    assert call["argv"] == [f"{instance}/ModOrganizer.exe", "run", "-e", "The Witcher 3"]
    assert call["overrides"] == "dxgi=n"  # as Heroic set it
    # ModSync's own Play picks the same one.
    assert game.build_plan(instance, found, play=True).command[-3:] == ["run", "-e", "The Witcher 3"]


def test_heroics_hook_script_quotes_what_it_is_given(machine):
    game = mo2games.get("witcher3")
    found = game.find_game()
    instance = make_instance(machine.tmp, "The Witcher 3: Wild Hunt", name="MO2 with a space")

    text = heroic_store.hook_script(game, found, instance)

    assert f"instance={shlex.quote(str(instance))}" in text
    assert f"game_dir={shlex.quote(str(machine.witcher))}" in text


# --- syncing the mods ----------------------------------------------------------------


def test_the_version_the_mods_are_for_is_recorded_from_the_game_not_a_launcher(machine):
    game = starfield()
    instance = game.use_instance(make_instance(machine.tmp, "Starfield"))
    with patch("modsync.mo2games.pe.build_and_product", return_value=((1, 16, 244, 0), "1.16.244.0")):
        game.folder.on_share(instance)
        assert json.loads((instance / game.vault_record).read_text()) == {
            "game_version": "1.16.244.0", "build": "1.16.244.0"}
        assert game.version_problem() is None
    with patch("modsync.mo2games.pe.build_and_product", return_value=((1, 15, 222, 0), "1.15.222.0")):
        assert "Starfield here is 1.15.222.0" in game.version_problem()

    vegas = mo2games.get("falloutnv")  # MO2-LINT knows only its launcher, whose version says nothing
    folder = machine.tmp / "Games/FNV"
    folder.mkdir()
    found = library.HeroicGame("1454587428", folder, "1.4", "1", False, machine.heroic)
    other = make_instance(machine.tmp, "New Vegas", name="MO2 FNV")
    with patch.object(vegas, "find_game", return_value=found):
        vegas.folder.on_share(other)
        assert vegas.game_version() is None
    assert not (other / vegas.vault_record).exists() and "record_owed" not in vegas._settings()


def test_every_games_folder_is_one_modsync_can_be_offered(machine):
    from modsync.sync import folders

    keys = [spec.key for spec in folders.specs()]
    assert "cp2077-mo2" in keys and "starfield-mo2" in keys and "dfu-mods" in keys
    assert len(keys) == len(set(keys))
    assert folders._spec_for_id("modsync-witcher3-mo2").label == "The Witcher 3: Wild Hunt mods (ModSync)"


# --- the command line ----------------------------------------------------------------


def test_the_games_command_lists_what_is_here_and_what_it_knows(machine, capsys):
    from modsync.__main__ import main

    assert main(["games"]) == 0
    out = capsys.readouterr().out
    assert "starfield" in out and "Steam; no MO2 instance" in out
    assert "dragonage2" in out and "Epic" in out and "falloutnv" not in out

    assert main(["games", "list", "--all"]) == 0
    assert "falloutnv" in capsys.readouterr().out
    assert main(["games", "no-such-game", "status"]) == 2
    assert main(["games", "starfield"]) == 2
    assert "modsync games starfield install" in capsys.readouterr().out


def test_status_says_where_the_game_and_its_mo2_are(machine, capsys):
    from modsync.__main__ import main

    assert main(["games", "starfield", "status"]) == 1  # no instance yet
    out = capsys.readouterr().out
    assert f"Starfield (Steam):     {machine.game_dir}" in out and "MO2 instance:          none" in out

    instance = starfield().use_instance(make_instance(machine.tmp, "Starfield"))
    assert main(["games", "starfield", "status"]) == 0
    out = capsys.readouterr().out
    assert f"MO2 instance:          {instance}" in out and "Play in Steam:" in out
    assert "modsync sync share starfield-mo2" in out
