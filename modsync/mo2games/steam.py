"""A game from Steam (the Steam half of :mod:`modsync.mo2games`).

MO2 runs in the game's own Proton prefix, with the Proton Steam uses for the
game, inside its Steam Linux Runtime (:func:`modsync.mo2.launch.build_plan`).
MO2-LINT installs MO2 and prepares the prefix through its bundled protontricks,
and leaves a compatibility tool of its own on the game that makes Steam's Play
button open MO2.

ModSync's launch hook (:mod:`modsync.launchhook`) goes in front of that, as it
does for Skyrim, but opens no window: ``modsync launch hub`` hands MO2's Play
straight back, and Steam runs it as the game's own launch, so Steam Cloud syncs
the saves when it exits. ModSync's own Play and Open MO2, and Nexus downloads,
go through Steam the same way while the hook is on and Steam is running: they
leave a note for the hub and ask Steam to launch the game
(:mod:`modsync.mo2.steam_downloads`).

Each function takes the :class:`~modsync.mo2games.ManagedGame` and the game as
Steam has it (a :class:`SteamGame`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from modsync import platforms, steamos
from modsync.games import Game
from modsync.mo2.launch import LaunchPlan
from modsync.steam import libraries, shortcuts
from modsync.steam.appmanifest import AppManifest

log = logging.getLogger(__name__)

LAUNCHER = "Steam"


@dataclass(frozen=True)
class SteamGame:
    """A game as Steam installed it."""

    appid: int
    install_path: Path
    steamapps: Path  # the library's steamapps folder
    version: str = ""  # Steam's build id
    store: str = "steam"
    pinned: bool = False  # Steam has no per-game "stay on this build" ModSync reads here

    @property
    def app_name(self) -> str:
        return str(self.appid)

    @property
    def prefix(self) -> Path:
        """The game's compat data folder, which holds the Wine prefix (``pfx``)."""
        return self.steamapps / "compatdata" / str(self.appid)

    @property
    def drive_c(self) -> Path | None:
        """The prefix's C: drive, or None before Steam has launched the game."""
        drive = self.prefix / "pfx" / "drive_c"
        return drive if drive.is_dir() else None


def _all_libraries() -> list[libraries.Library]:
    return libraries.all_libraries(platforms.current().steam_roots())


def find(game: Game) -> SteamGame | None:
    """The game in Steam's libraries, when its folder is there."""
    app = libraries.find_app(_all_libraries(), game.appid) if game.appid else None
    if app is None or not app.install_path.is_dir():
        return None
    try:
        build = AppManifest.load(app.library.steamapps / f"appmanifest_{game.appid}.acf").buildid
    except (OSError, ValueError):
        build = None
    return SteamGame(game.appid, app.install_path, app.library.steamapps, str(build or ""))


def installed_ids() -> set[int]:
    """The ids of everything installed in Steam's libraries, from the
    libraries' own lists (no manifest is read)."""
    found: set[int] = set()
    for library in _all_libraries():
        found |= set(library.app_ids)
        if not library.app_ids:  # the old library format lists no apps
            found |= {app.appid for app in libraries.iter_apps(library)}
    return found


def not_ready(mg, game: SteamGame) -> str | None:
    """Why nothing can run in the game's prefix yet, or None."""
    if game.drive_c is None:
        return f"{mg.short}'s prefix isn't set up yet. Launch the game once from Steam first."
    return None


def install_blocked(mg, game: SteamGame) -> str | None:
    """MO2-LINT changes Steam's settings for the game, which Steam writes back
    from memory when it exits. The Steam Frame can't close Steam: MO2-LINT
    leaves it running there, and Steam picks the change up at the next boot."""
    if shortcuts.steam_is_running() and not steamos.is_steam_frame():
        return "Close Steam first: the installer changes the game's settings in Steam and its Proton prefix."
    return None


# --- launching ----------------------------------------------------------------


def build_plan(mg, instance: Path, game: SteamGame, *, play: bool = False, download: str | None = None) -> LaunchPlan:
    from modsync.mo2 import launch

    def target(sections: dict, game_dir: Path) -> tuple[list[str], str]:
        return mg.play_target(sections, game_dir, game.store)

    try:
        return launch.build_plan(instance, play=play, download=download, game=mg.game, target=target)
    except RuntimeError as e:
        if isinstance(e, mg.Error):
            raise
        raise mg.Error(str(e)) from e


def prepare(mg, game: SteamGame, plan: LaunchPlan) -> None:
    """What Open MO2 does before MO2 starts, as for Skyrim: a current VC++
    runtime next to MO2 when the prefix's own is too old for it (a hand-made
    or synced instance), and MO2's marker for a portable instance."""
    from modsync.mo2 import launch, vcruntime

    try:
        vcruntime.ensure_instance_runtime(plan.cwd, system32=launch.plan_system32(plan))
    except (OSError, RuntimeError, ValueError) as exc:
        # MO2 may still work if the prefix has a current runtime; don't block it.
        log.warning("could not add the VC++ runtime to %s: %s", plan.cwd, exc)
    launch.mark_portable(plan)


# --- Steam's Play button -------------------------------------------------------


def hook_status(mg, game: SteamGame, instance: Path | None):
    from modsync import launchhook

    return launchhook.status(game.appid)


def enable_hook(mg, game: SteamGame, instance: Path) -> list[str]:
    from modsync import launchhook

    try:
        return [launchhook.enable(game.appid)]
    except RuntimeError as e:
        raise mg.Error(str(e)) from e


def disable_hook(mg, game: SteamGame) -> str:
    from modsync import launchhook

    try:
        return launchhook.disable(game.appid)
    except RuntimeError as e:
        raise mg.Error(str(e)) from e


def refresh_hook(mg, game: SteamGame, instance: Path) -> list[str]:
    """Steam's hook asks ModSync what to run at every launch, so only its
    script can go out of date."""
    from modsync import launchhook

    note = launchhook.upgrade(game.appid)
    return [note] if note else []


def through_hook(mg, game: SteamGame, instance: Path) -> bool:
    """Whether a launch of the game in Steam would run ModSync's hook now."""
    from modsync import launchhook

    hook = launchhook.status(game.appid)
    return hook.installed and hook.selected and hook.steam_running


def _target(mg, game: SteamGame, instance: Path):
    from modsync.mo2 import steam_downloads

    return steam_downloads.SteamDownloads(mg.game, mg.build_plan(instance, game))


def ask_store(mg, game: SteamGame, note: str) -> None:
    """Have Steam launch the game, with ``note`` (an nxm:// link, or PLAY or
    OPEN) left for 'modsync launch hub' to take."""
    from modsync.mo2 import steam_downloads

    steam_downloads.ask_steam(mg.game, note)


# --- Nexus downloads ------------------------------------------------------------


def downloads_target(mg, game: SteamGame, instance: Path):
    return _target(mg, game, instance)


def download(mg, game: SteamGame, instance: Path, url: str, *, poll: float = 2.0, on_wait=None) -> str:
    from modsync.mo2 import steam_downloads

    try:
        return steam_downloads.download(mg.game, url, poll=poll, on_wait=on_wait)
    except RuntimeError as e:
        if isinstance(e, mg.Error):
            raise
        raise mg.Error(str(e)) from e


# --- installing MO2 -----------------------------------------------------------


def mo2lint_install(mg, game: SteamGame, backend, dest: Path, on_output):
    """MO2-LINT's install for a Steam game: MO2, its config, the prefix steps
    (with its bundled protontricks) and its own compatibility tool on the
    game, which makes Steam's Play open MO2."""
    return backend.install(mg.game, dest, launcher="steam", on_output=on_output)


def prepare_prefix(mg, game: SteamGame, on_output=None) -> None:
    """MO2-LINT prepared the prefix itself. What it can leave undone, an old
    VC++ runtime, :func:`prepare` covers each time MO2 starts."""
