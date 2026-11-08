"""Nexus Mods download links (modsync.nxm): ModSync as the desktop's handler,
and which program each link goes to."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from modsync import nxm

LINK = "nxm://cyberpunk2077/mods/2987/files/168466?key=abc&expires=1794291732&user_id=1"
MO2LINT = "mo2lint_nxm-handler.desktop"
MO2LINT_EXEC = """Exec=bash -c '"$HOME/.local/share/mo2-lint/nxm-handler" "$@"' "$HOME/.local/share/mo2-lint/nxm-handler" %u"""


class FakeXdgMime:
    """xdg-mime's default for nxm://, as run_host would reach it."""

    def __init__(self, default: str | None = MO2LINT):
        self.default = default
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **_kw):
        self.calls.append(cmd)
        if cmd[:3] == ["xdg-mime", "query", "default"]:
            return subprocess.CompletedProcess(cmd, 0, (self.default or "") + "\n", "")
        if cmd[:2] == ["xdg-mime", "default"]:
            self.default = cmd[2]
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    nxm._settings_path().unlink(missing_ok=True)
    xdg = FakeXdgMime()
    with patch.object(nxm.background, "run_host", side_effect=xdg), \
            patch("modsync.launchhook.modsync_command", return_value=["/usr/bin/flatpak", "run", "io.github.skjiisa.ModSync"]):
        yield xdg
    nxm._settings_path().unlink(missing_ok=True)


def test_the_game_is_the_links_first_part():
    assert nxm.game_of(LINK) == "cyberpunk2077"
    assert nxm.game_of("NXM://SkyrimSpecialEdition/mods/1/files/2") == "skyrimspecialedition"
    assert nxm.game_of("https://www.nexusmods.com/") is None


@pytest.mark.parametrize("arg", ["/usr/bin/modsync", "/home/me/My Games/modsync", 'say "hi" $HOME `x`', "100%", "a\\b"])
def test_an_exec_line_comes_back_as_the_same_arguments(tmp_path, arg):
    entry = tmp_path / "e.desktop"
    entry.write_text(nxm.render_entry([arg, "--flag"]))
    assert nxm.entry_command(entry, LINK) == [arg, "--flag", "nxm", LINK]


def test_mo2lints_entry_gets_the_link_where_it_says(tmp_path):
    entry = tmp_path / MO2LINT
    entry.write_text(f"[Desktop Entry]\nType=Application\n{MO2LINT_EXEC}\nMimeType=x-scheme-handler/nxm;\n")
    assert nxm.entry_command(entry, LINK) == [
        "bash", "-c", '"$HOME/.local/share/mo2-lint/nxm-handler" "$@"', "$HOME/.local/share/mo2-lint/nxm-handler", LINK]


def test_enable_records_the_handler_it_replaces_and_disable_puts_it_back(desktop):
    message = nxm.enable()
    assert MO2LINT in message
    assert desktop.default == nxm.DESKTOP_ID
    text = nxm.entry_path().read_text()
    assert "Exec=/usr/bin/flatpak run io.github.skjiisa.ModSync nxm %u" in text
    assert "MimeType=x-scheme-handler/nxm;" in text
    assert nxm.status() == nxm.Status(True, nxm.DESKTOP_ID, MO2LINT)

    nxm.enable()  # again: still remembers MO2-LINT's, not itself
    assert nxm.status().previous == MO2LINT

    assert MO2LINT in nxm.disable()
    assert desktop.default == MO2LINT
    assert not nxm.entry_path().exists()
    assert not nxm.status().enabled


def test_refresh_takes_the_links_back_from_mo2lint_only(desktop):
    assert nxm.refresh() == []  # never enabled: nothing to keep
    nxm.enable()

    desktop.default = MO2LINT  # MO2-LINT reinstalled
    (note,) = nxm.refresh()
    assert MO2LINT in note and desktop.default == nxm.DESKTOP_ID

    desktop.default = "some-other-handler.desktop"  # the user's own choice
    assert nxm.refresh() == []
    assert desktop.default == "some-other-handler.desktop"
    assert nxm.status().summary().startswith("on, but some-other-handler.desktop opens them")


def test_refresh_rewrites_the_entry_when_modsync_starts_differently(desktop):
    nxm.enable()
    with patch("modsync.launchhook.modsync_command", return_value=["/usr/bin/modsync"]), \
            patch.object(nxm.config, "in_flatpak", return_value=True):
        nxm.refresh()
    assert "Exec=/usr/bin/modsync nxm %u" in nxm.entry_path().read_text()


class FakeHandler:
    def __init__(self, claims: bool):
        self._claims = claims
        self.downloads: list[str] = []

    def make(self):
        return nxm.GameHandler("Cyberpunk 2077", lambda: self._claims, self.download)

    def download(self, url):
        self.downloads.append(url)
        return "sent"


def test_a_game_modsync_runs_gets_its_own_links(desktop):
    handler = FakeHandler(claims=True)
    with patch.dict(nxm.HANDLERS, {"cyberpunk2077": handler.make}):
        assert nxm.handle(LINK) == "sent"
    assert handler.downloads == [LINK]


@pytest.mark.parametrize("link", [LINK, "nxm://skyrimspecialedition/mods/1/files/2?key=k"])
def test_other_links_go_to_the_handler_from_before(desktop, link):
    nxm.enable()
    entry = Path.home() / ".local/share/applications" / MO2LINT
    entry.write_text(f"[Desktop Entry]\n{MO2LINT_EXEC}\n")
    with patch.dict(nxm.HANDLERS, {"cyberpunk2077": FakeHandler(claims=False).make}), \
            patch.object(nxm.subprocess, "Popen") as popen:
        assert MO2LINT in nxm.handle(link)
    assert popen.call_args.args[0][-1] == link
    assert popen.call_args.kwargs["start_new_session"]


def test_a_link_nobody_else_handles_says_so(desktop):
    with pytest.raises(nxm.NxmError, match="no other program"):
        nxm.handle("nxm://skyrimspecialedition/mods/1/files/2")
    nxm.enable()
    with pytest.raises(nxm.NxmError, match="isn't installed anymore"):
        nxm.handle("nxm://skyrimspecialedition/mods/1/files/2")


def test_something_that_isnt_a_link_is_refused(desktop):
    with pytest.raises(nxm.NxmError):
        nxm.handle("https://example.com")
    with pytest.raises(nxm.NxmError):
        nxm.handle("nxm://cyberpunk2077/mods/1\n--evil")


def test_the_cli_says_a_failure_out_loud(desktop, capsys):
    from modsync import cli

    with patch.object(cli, "_notify") as notify:
        assert cli.nxm(["nxm://skyrimspecialedition/mods/1/files/2"]) == 1
    assert "didn't start" in notify.call_args.args[0]


def test_settings_survive_as_json(desktop):
    nxm.enable()
    data = json.loads(nxm._settings_path().read_text())
    assert data["previous"] == MO2LINT and data["enabled"] is True
