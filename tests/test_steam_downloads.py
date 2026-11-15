"""Nexus downloads for Skyrim, and any Steam game ModSync runs MO2 for
(modsync.mo2.steam_downloads): which way a link reaches MO2, the note Steam's
launch hands to the hub, and the hub opening MO2 with it."""

import io
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from modsync import launchhook, nxm
from modsync.games import DAGGERFALL, SKYRIM_SE
from modsync.mo2 import downloads, steam_downloads
from modsync.mo2.launch import LaunchPlan

LINK = "nxm://skyrimspecialedition/mods/266/files/1000?key=k&expires=1&user_id=2"


def make_plan(tmp_path: Path) -> LaunchPlan:
    """Open MO2 with the link, as build_plan lays it out for Steam's Proton
    inside its Steam Linux Runtime."""
    instance = tmp_path / "MO2"
    instance.mkdir(exist_ok=True)
    (instance / "ModOrganizer.exe").touch()
    compat = tmp_path / "compatdata" / "489830"
    (compat / "pfx").mkdir(parents=True, exist_ok=True)
    proton, exe = tmp_path / "Proton", str(instance / "ModOrganizer.exe")
    argv = [str(tmp_path / "SLR" / "_v2-entry-point"), "--verb=run", "--", str(proton / "proton"), "run", exe, LINK]
    return LaunchPlan(argv, {"STEAM_COMPAT_DATA_PATH": str(compat)}, instance, "Mod Organizer 2", False, proton,
                      [exe, LINK])


class Downloading:
    """steam_downloads.download for Skyrim with the plan, the hook and every
    way out stubbed."""

    def __init__(self, tmp_path, *, running=False, hook_on=False, steam_running=True, launching=False):
        self.plan = make_plan(tmp_path)
        self.launching = launching  # Steam's reaper is running a launch of the game
        self.opened = subprocess.CompletedProcess([], 0, "", "")  # what xdg-open answers
        hook = SimpleNamespace(installed=hook_on, selected=hook_on, steam_running=steam_running)
        self.patches = [
            patch.object(steam_downloads, "state_dir", return_value=tmp_path / "state"),
            patch.object(downloads, "shared_dir", return_value=tmp_path / "shared"),
            patch.object(steam_downloads, "_plan", return_value=self.plan),
            patch.object(steam_downloads.downloads, "mo2_running", return_value=running),
            patch.object(launchhook, "status", return_value=hook),
            patch.object(steam_downloads.background, "run_host", side_effect=self._host),
            patch.object(steam_downloads.background, "in_flatpak", return_value=False),
            patch.object(steam_downloads.subprocess, "Popen"),
            patch("modsync.mo2.launch.Launcher.start", return_value="Starting Mod Organizer 2…"),
        ]

    def _host(self, cmd, **_kw):
        if cmd[0] == "pgrep":
            return subprocess.CompletedProcess(cmd, 0 if self.launching else 1, "", "")
        return self.opened

    def opens(self) -> list[list[str]]:
        """What ModSync asked the host to open (Steam launches)."""
        return [c.args[0] for c in self.run_host.call_args_list if c.args[0][0] == "xdg-open"]

    def __enter__(self):
        mocks = [p.start() for p in self.patches]
        self.mo2_running, self.run_host, self.popen, self.start = mocks[3], mocks[5], mocks[7], mocks[8]
        self.popen.return_value.wait.return_value = 0
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()

    def download(self, url=LINK, **kw):
        return steam_downloads.download(SKYRIM_SE, url, **kw)


def test_an_open_mo2_gets_the_link_from_a_second_mo2_in_the_same_prefix(tmp_path):
    with Downloading(tmp_path, running=True) as d:
        assert d.download() == "Passed the download to Mod Organizer 2."
        argv = d.popen.call_args.args[0]
        env = d.popen.call_args.kwargs["env"]
    exe = str(d.plan.cwd / "ModOrganizer.exe")
    assert argv == [str(tmp_path / "SLR" / "_v2-entry-point"), "--verb=runinprefix", "--",
                    str(tmp_path / "Proton" / "proton"), "runinprefix", exe, LINK]
    assert env["STEAM_COMPAT_DATA_PATH"] == d.plan.env["STEAM_COMPAT_DATA_PATH"]
    assert not d.start.called


def test_an_open_mo2_that_refuses_the_link_says_so(tmp_path):
    with Downloading(tmp_path, running=True) as d:
        d.popen.return_value.wait.return_value = 3
        with pytest.raises(RuntimeError, match="exit code 3"):
            d.download()


def test_a_closed_mo2_opens_through_steam_while_the_hook_is_on(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        assert "through Steam" in d.download()
        assert steam_downloads.note_path(SKYRIM_SE).read_text() == LINK + "\n"
        assert d.opens() == [["xdg-open", "steam://rungameid/489830"]]
        assert not d.start.called


@pytest.mark.parametrize("hook_on, steam_running", [(False, True), (True, False)])
def test_otherwise_mo2_opens_directly_with_the_link(tmp_path, hook_on, steam_running):
    with Downloading(tmp_path, hook_on=hook_on, steam_running=steam_running) as d:
        assert d.download() == "Starting Mod Organizer 2…"
        assert d.start.call_args.args == (d.plan,)
        assert not steam_downloads.note_path(SKYRIM_SE).exists()


def test_a_launch_steam_couldnt_be_asked_for_leaves_nothing_behind(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        d.opened = subprocess.CompletedProcess([], 4, "", "no handler for steam://")
        with pytest.raises(RuntimeError, match="no handler"):
            d.download()
        assert not steam_downloads.note_path(SKYRIM_SE).exists()
        d.opened = subprocess.CompletedProcess([], 0, "", "")
        with patch("time.sleep", side_effect=AssertionError("waited")):
            assert "through Steam" in d.download()


def test_a_link_while_mo2_is_starting_through_steam_waits_for_it(tmp_path):
    with Downloading(tmp_path, hook_on=True) as d:
        d.download()  # its note is still waiting for the hub
        d.mo2_running.side_effect = [False, False, True]
        assert d.download(LINK + "2", poll=0) == "Passed the download to Mod Organizer 2."
        assert len(d.opens()) == 1
        assert d.popen.call_args.args[0][-1] == LINK + "2"


def test_a_link_while_steam_launches_the_game_for_play_waits_for_its_mo2(tmp_path):
    """Steam would ignore a launch for the link, leaving its note for the next Play."""
    with Downloading(tmp_path, hook_on=True, launching=True) as d:
        d.mo2_running.side_effect = [False, False, True]
        assert d.download(poll=0) == "Passed the download to Mod Organizer 2."
        assert d.opens() == [] and not d.start.called
        assert not steam_downloads.note_path(SKYRIM_SE).exists()


def test_a_link_gives_up_on_a_steam_launch_that_never_opens_mo2(tmp_path):
    with Downloading(tmp_path, hook_on=True, launching=True) as d, patch.object(downloads, "BUSY_WAIT", 0):
        with pytest.raises(RuntimeError, match="Steam is already launching Skyrim Special Edition"):
            d.download(poll=0)
        assert d.opens() == [] and not steam_downloads.note_path(SKYRIM_SE).exists()


def test_the_handler_and_the_hub_meet_in_a_folder_both_copies_share(tmp_path, monkeypatch):
    """The Flatpak's state folder is its own; its data folder is the one a
    copy run on the host uses too (config.shares_flatpak_data). The handler
    may be one copy and Steam's hook start the other."""
    from modsync import config

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config, "flatpak_installed", lambda: True)
    paths = set()
    for flatpak, data, state in ((True, config.flatpak_home() / "data", config.flatpak_home() / ".local/state"),
                                 (False, tmp_path / ".local/share", tmp_path / ".local/state")):
        monkeypatch.setattr(config, "in_flatpak", lambda flatpak=flatpak: flatpak)
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        paths.add((steam_downloads.note_path(SKYRIM_SE), steam_downloads._record_path(SKYRIM_SE)))
    assert len(paths) == 1


# --- the note, as the hub takes it ----------------------------------------------------


@pytest.fixture
def state(tmp_path):
    with patch.object(downloads, "shared_dir", return_value=tmp_path):
        yield tmp_path


def leave(text: str, *, ago: float = 0) -> Path:
    note = steam_downloads.note_path(SKYRIM_SE)
    note.write_text(text + "\n")
    os.utime(note, (time.time() - ago,) * 2)
    return note


def test_the_hub_takes_a_link_once_and_marks_when_it_did(state):
    note = leave(LINK, ago=30)
    assert steam_downloads.take_link(SKYRIM_SE) == LINK
    taken = steam_downloads.taken_path(SKYRIM_SE)
    assert not note.exists() and taken.read_text() == ""
    assert time.time() - taken.stat().st_mtime < 5  # MO2's launch begins now, not when the note was left
    assert steam_downloads.take_link(SKYRIM_SE) is None


@pytest.mark.parametrize("text, ago", [(LINK, downloads.NOTE_TTL + 5), ("modsync:play", 0)])
def test_a_stale_or_unknown_note_is_taken_but_not_followed(state, text, ago):
    note = leave(text, ago=ago)
    assert steam_downloads.take_link(SKYRIM_SE) is None
    assert not note.exists()


def hub(appid):
    from modsync.cli import _open_for_download

    out = io.BytesIO()
    return _open_for_download(appid, out), out.getvalue()


def test_the_hub_hands_the_hook_open_mo2_with_the_link(state, tmp_path):
    leave(LINK)
    plan = make_plan(tmp_path)
    with patch.object(steam_downloads, "prepare", return_value=plan) as prepare:
        (opened, reason), handoff = hub(SKYRIM_SE.appid)
    assert (opened, reason) == (True, "")
    assert prepare.call_args.args == (SKYRIM_SE, LINK)
    assert handoff == launchhook.encode_handoff(plan)


def test_without_a_note_the_hub_goes_on_as_usual(state):
    with patch.object(steam_downloads, "prepare") as prepare:
        assert hub(SKYRIM_SE.appid) == ((False, ""), b"")
        leave(LINK)
        assert hub(DAGGERFALL.appid) == ((False, ""), b"")  # not an MO2 game: its note isn't looked for
    prepare.assert_not_called()


def test_an_mo2_that_cant_open_for_the_link_keeps_the_window_open_with_why(state):
    leave(LINK)
    with patch.object(steam_downloads, "prepare", side_effect=RuntimeError("Steam is updating Skyrim.")):
        (opened, reason), handoff = hub(SKYRIM_SE.appid)
    assert not opened and handoff == b""
    assert "Steam is updating Skyrim." in reason


# --- which links are ModSync's ----------------------------------------------------------


def test_skyrim_links_are_modsyncs_once_it_runs_skyrims_mo2(tmp_path):
    from modsync.state import State

    State.update(instance_path=None)
    assert not steam_downloads.takes_downloads(SKYRIM_SE)
    (tmp_path / "ModOrganizer.exe").touch()
    State.update(instance_path=str(tmp_path))
    assert steam_downloads.takes_downloads(SKYRIM_SE)
    State.update(instance_path=None)


def test_a_skyrim_link_goes_to_skyrims_mo2_or_on_to_the_handler_from_before():
    assert "skyrimspecialedition" in nxm.HANDLERS
    with patch.object(steam_downloads, "download", return_value="ok") as download, \
            patch.object(nxm, "forward", return_value="forwarded") as forward:
        with patch.object(steam_downloads, "takes_downloads", return_value=True):
            assert nxm.handle(LINK) == "ok"
        assert download.call_args.args == (SKYRIM_SE, LINK)
        with patch.object(steam_downloads, "takes_downloads", return_value=False):
            assert nxm.handle(LINK) == "forwarded"
        assert forward.call_args.args == (LINK,)


def test_turning_a_play_hook_on_takes_the_links_unless_modsync_has_them():
    active, inactive = nxm.Status(True, nxm.DESKTOP_ID, None), nxm.Status(False, "x.desktop", None)
    with patch.object(nxm, "status", return_value=active), patch.object(nxm, "enable") as enable:
        assert nxm.claim_links() is None
    enable.assert_not_called()
    with patch.object(nxm, "status", return_value=inactive), patch.object(nxm, "enable", return_value="Taken."):
        assert nxm.claim_links() == "Taken."
    with patch.object(nxm, "status", return_value=inactive), \
            patch.object(nxm, "enable", side_effect=nxm.NxmError("no xdg-mime")):
        assert "'modsync nxm enable' retries" in nxm.claim_links()


# --- MO2 in Steam's prefix ----------------------------------------------------------------


def test_an_mo2_proton_started_is_found_through_its_z_drive(tmp_path):
    instance = tmp_path / "MO2"
    instance.mkdir()
    prefix = tmp_path / "pfx"
    (prefix / "dosdevices").mkdir(parents=True)
    (prefix / "dosdevices" / "z:").symlink_to("/")
    windows = "Z:" + str(instance / "ModOrganizer.exe").replace("/", "\\")

    def run_host(cmd, **_kw):
        if cmd[0] == "cat":
            return subprocess.CompletedProcess(cmd, 0, f"WINEPREFIX={prefix}/\0", "")
        return subprocess.CompletedProcess(cmd, 0, f"1000 {windows} -p Default\n", "")

    with patch.object(downloads.background, "run_host", side_effect=run_host):
        assert downloads.mo2_running(prefix, instance)
        assert not downloads.mo2_running(prefix, tmp_path / "Other MO2")
