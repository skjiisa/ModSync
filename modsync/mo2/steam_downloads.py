"""Nexus downloads for the Steam games whose Mod Organizer 2 ModSync starts
(see :mod:`modsync.mo2.downloads` and :mod:`modsync.nxm`).

While Steam's launch hook (:mod:`modsync.launchhook`) is on and Steam is
running, a closed MO2 opens through Steam, the way Steam's Play would.
ModSync leaves the link in a note and asks Steam to launch the game. The hook
runs ``modsync launch hub``, which takes the note (:func:`take_link`) and hands
back Open MO2 with the link instead of playing, with no window and no
countdown. Steam tracks the session, so cloud saves still sync if the game is
then played from MO2. MO2-LINT's own handler would launch the game through
Steam too, but the hook would play it.

Without the hook, or with Steam closed, ModSync opens MO2 itself. The game
starts Steam on its own if it's then played from MO2.

Skyrim Special Edition is the only Steam game ModSync runs MO2 for so far.
Another one needs its instance in :func:`instance` and its launch in
:func:`_plan`; everything else here goes by its :class:`~modsync.games.Game`
entry.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from modsync import background
from modsync.games import SKYRIM_SE, Game
from modsync.logging_setup import state_dir
from modsync.mo2 import downloads
from modsync.mo2.launch import LaunchPlan, host_command, proton_command


# --- what differs between games ----------------------------------------------------


def instance(game: Game) -> Path | None:
    """The MO2 instance ModSync runs for the game, or None for none."""
    if game.appid != SKYRIM_SE.appid:
        return None
    from modsync.state import State

    path = State.load().instance_path
    return Path(path).expanduser() if path else None


def _plan(game: Game, url: str, *, prepare: bool) -> LaunchPlan:
    """MO2's launch with the link. ``prepare``: also everything Open MO2 does
    before MO2 starts (for a launch that starts it, not one that only says
    where it runs)."""
    if game.appid != SKYRIM_SE.appid:
        raise RuntimeError(f"ModSync doesn't run Mod Organizer 2 for {game.name}.")
    if prepare:
        from modsync.service import ModSyncService

        return ModSyncService().prepare_mo2(download=url)[0]
    from modsync.mo2.launch import build_plan

    return build_plan(instance(game), download=url)


# --- the same for every Steam game -------------------------------------------------


def takes_downloads(game: Game) -> bool:
    """Whether the game's nxm:// links are ModSync's to open: there's an MO2
    instance for it."""
    path = instance(game)
    return path is not None and (path / "ModOrganizer.exe").is_file()


def note_path(game: Game) -> Path:
    """Where ModSync leaves the link for the hub that Steam's launch runs."""
    return state_dir() / f"steam-{game.appid}-download.txt"


def taken_path(game: Game) -> Path:
    return note_path(game).with_suffix(".taken")


def _record_path(game: Game) -> Path:
    return state_dir() / f"steam-{game.appid}-download-launch"


def take_link(game: Game) -> str | None:
    """For the hub: the link ModSync left for this launch, taken once, or None."""
    return downloads.take_note(note_path(game), taken_path(game))


def prepare(game: Game, url: str) -> LaunchPlan:
    """For the hub: Open MO2 with the link, ready to hand to the hook."""
    return _plan(game, url, prepare=True)


@dataclass
class SteamDownloads:
    """A Steam game's MO2 as a download reaches it."""

    game: Game
    plan: LaunchPlan  # MO2 with the link, which also says which prefix it runs in

    @property
    def record(self) -> Path:
        return _record_path(self.game)

    @property
    def note(self) -> Path:
        return note_path(self.game)

    @property
    def taken(self) -> Path:
        return taken_path(self.game)

    def running(self) -> bool:
        return downloads.mo2_running(Path(self.plan.env["STEAM_COMPAT_DATA_PATH"]) / "pfx", self.plan.cwd)

    def pass_link(self, url: str) -> str:
        """Proton's default verbs set up the prefix and wait on the program;
        runinprefix runs it alone, in the same Wine session as the open MO2
        (it exits once that MO2 has the link)."""
        exe = str(self.plan.cwd / "ModOrganizer.exe")
        argv, env = host_command(self.plan, proton_command(self.plan, [exe, url], verb="runinprefix"))
        path = state_dir() / "mo2-launch.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as output:
            output.write(b"\n--- ModSync: passing a download to Mod Organizer 2 ---\n")
            output.flush()
            proc = subprocess.Popen(argv, cwd=self.plan.cwd, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            return "Passed the download to Mod Organizer 2, which is slow to answer."
        if code != 0:
            raise RuntimeError(f"Mod Organizer 2 didn't take the download (exit code {code}). Details: {path}")
        return "Passed the download to Mod Organizer 2."

    def through_hook(self) -> bool:
        from modsync import launchhook

        hook = launchhook.status(self.game.appid)
        return hook.installed and hook.selected and hook.steam_running

    def ask_hook(self, url: str) -> str:
        self.note.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.note.with_suffix(".tmp")
        tmp.write_text(url + "\n", encoding="utf-8")
        tmp.replace(self.note)
        try:
            result = background.run_host(["xdg-open", f"steam://rungameid/{self.game.appid}"], timeout=15)
        except (OSError, subprocess.SubprocessError) as e:
            self.note.unlink(missing_ok=True)  # no launch will take it
            raise RuntimeError(f"Couldn't ask Steam to launch {self.game.name}: {e}") from e
        if result.returncode != 0:
            self.note.unlink(missing_ok=True)
            raise RuntimeError(f"Couldn't ask Steam to launch {self.game.name}: "
                               f"{(result.stderr or '').strip() or result.returncode}")
        return "Opening Mod Organizer 2 through Steam to download the file…"

    def open_direct(self, url: str) -> str:
        from modsync.mo2.launch import Launcher

        return Launcher().start(_plan(self.game, url, prepare=True))


def download(game: Game, url: str, *, poll: float = 2.0) -> str:
    """Hand a Nexus download to the game's MO2 (see the module's docstring).
    Returns what happened."""
    return downloads.download(SteamDownloads(game, _plan(game, url, prepare=False)), url, poll=poll)
