"""Cyberpunk 2077, modded with Mod Organizer 2: a game from the table
(:mod:`modsync.mo2games`) with a few things of its own.

Everything a game gets, Cyberpunk gets from :class:`~modsync.mo2games.ManagedGame`:
found in Heroic (GOG or Epic) or Steam, MO2 installed with MO2-LINT, Play and
Open MO2 in the game's own prefix, the store's Play button through MO2, Nexus
downloads, and mods sync. What is Cyberpunk's own:

MO2's Cyberpunk plugin (MO2's basic_games) knows the game: its default
executable runs ``Cyberpunk2077.exe --launcher-skip -modded``, skipping
REDlauncher and deploying REDmod first, so Play prefers that one. Cyber Engine
Tweaks and RED4ext are loaded through proxy DLLs (``version.dll``,
``winmm.dll``) next to the game, which Wine ignores in favour of its own unless
told otherwise, so every launch sets :data:`DLL_OVERRIDES`. Both are facts
about the game, in :data:`modsync.games.QUIRKS`.

Those DLLs reach the game folder through the RootBuilder plugin, which MO2
doesn't ship: :meth:`Cyberpunk.after_install` adds it, set up the way the
Cyberpunk plugin would set it on MO2's first start (copy mode: the files are
copied in for the session and taken out again when the game exits).

:data:`MODS_FOLDER` syncs the instance's mods, profiles and downloads with
paired machines (see :mod:`modsync.sync.folders`), under the same rules as
Skyrim's vault, plus what the game, CET, RED4ext and vkd3d write into
``overwrite`` while playing. Only an instance handed to ModSync (``install`` or
``use``) is ever synced. Saves don't: the game syncs them across stores itself.
The machine that starts syncing records the game version the mods are for, and
a different version here is reported, never acted on.

The functions at the end are the module as it was before the table: each
one is :data:`GAME`'s method of the same name.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
import zipfile
from pathlib import Path

from modsync import config, mo2games
from modsync.games import CYBERPUNK
from modsync.heroic import hook as heroic_hook  # noqa: F401 (the hook, as this module's users reach it)
from modsync.mo2 import ini
from modsync.mo2.launch import with_dll_overrides as _with_dll_overrides
from modsync.mo2games import heroic as heroic_store

log = logging.getLogger(__name__)

DLL_OVERRIDES = ",".join(CYBERPUNK.dll_overrides) + "=n,b"

MO2LINT_ENV = heroic_store.MO2LINT_ENV
# MO2-LINT's own prefix steps for Cyberpunk, less vcrun2022 (see mo2games.heroic.tricks).
TRICKS = (*mo2games.BASE_TRICKS, *CYBERPUNK.tricks)

ROOTBUILDER_VERSION = "5.1.1"
ROOTBUILDER_URL = ("https://github.com/Kezyma/ModOrganizer-Plugins/releases/download/rootbuilder/"
                   f"rootbuilder.{ROOTBUILDER_VERSION}.zip")
ROOTBUILDER_SHA256 = "902f1edf7ccae8fa625d4ea50b8fd36a0540cda121ad584989e4b1ed977046b1"
# What the Cyberpunk plugin sets on MO2's first start (its _rootbuilder_settings),
# written ahead so Play works before MO2 has ever been opened. Only keys that
# aren't set yet are written, so a choice made in MO2 stays.
ROOTBUILDER_SETTINGS = {
    "RootBuilder\\enabled": "true",
    "RootBuilder\\usvfsmode": "false",
    "RootBuilder\\linkmode": "false",
    "RootBuilder\\linkonlymode": "false",
    "RootBuilder\\backup": "true",
    "RootBuilder\\cache": "true",
    "RootBuilder\\autobuild": "true",
    "RootBuilder\\redirect": "false",
    "RootBuilder\\installer": "false",
    "RootBuilder\\exclusions": '"archive,setup_redlauncher.exe,tools"',
    "RootBuilder\\linkextensions": '"dll,exe"',
    "Cyberpunk%202077%20Support%20Plugin\\configure_RootBuilder": "false",
}

VAULT_RECORD = "modsync-cp2077-vault.json"  # which game version the synced mods are for; synced

# Machine-local files the game, Cyber Engine Tweaks, RED4ext and vkd3d write
# into MO2's overwrite while playing (seen on the desktop), and logs anywhere.
# Before the content below, since Syncthing takes the first pattern that matches.
PLAY_LEFTOVERS = (
    "*.log",
    "*.sqlite3*",  # CET mods' databases and their journals, written while the game runs
    "/overwrite/r6/cache",  # the game's own caches, copied there by REDmod's deploy
    "/overwrite/Root/bin/x64/vkd3d-proton.cache*",
    "/overwrite/bin/x64/*.exe-*.txt",  # the crash reporter's registrations
    "/overwrite/Root/red4ext/logs",
)


class CyberpunkError(mo2games.GameError):
    pass


def _rootbuilder_zip() -> Path:
    """The pinned RootBuilder release, downloaded once and checked."""
    path = config.data_dir() / "downloads" / f"rootbuilder.{ROOTBUILDER_VERSION}.zip"
    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == ROOTBUILDER_SHA256:
        return path
    log.info("downloading %s", ROOTBUILDER_URL)
    with urllib.request.urlopen(ROOTBUILDER_URL, timeout=60) as response:
        data = response.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != ROOTBUILDER_SHA256:
        raise CyberpunkError(f"RootBuilder's download didn't match its checksum ({digest}).")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def install_rootbuilder(instance: Path) -> None:
    """RootBuilder in the instance's plugins folder, set up for Cyberpunk."""
    plugins = (instance / "plugins").resolve()
    with zipfile.ZipFile(_rootbuilder_zip()) as archive:
        for member in archive.infolist():
            target = (plugins / member.filename).resolve()
            if not member.filename.startswith("rootbuilder/") or not target.is_relative_to(plugins):
                continue
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    ini.add_missing(instance / "ModOrganizer.ini", "Plugins", ROOTBUILDER_SETTINGS)


def setup_finished(instance: Path) -> bool:
    """Whether ``install_mo2`` got to its last step for this instance."""
    return (instance / "plugins" / "rootbuilder" / "__init__.py").is_file()


class Cyberpunk(mo2games.ManagedGame):
    """Cyberpunk 2077 keeps the names it had before the table (its settings
    file, its synced folder, its command, its page in the app), and adds
    RootBuilder to the instance."""

    Error = CyberpunkError
    short = "Cyberpunk"
    slug = "cyberpunk"
    ui_key = "cyberpunk"
    command = "modsync cyberpunk"
    settings_file = "cyberpunk.json"
    sync_key = "cp2077-mo2"
    vault_record = VAULT_RECORD
    leftovers = PLAY_LEFTOVERS
    # Frameworks like CET and RED4ext need a version they support, but whether
    # to change versions (Heroic can install older GOG builds) is the player's call.
    version_advice = ("Mods made for another version may not work, CET and RED4ext "
                      "especially. Heroic can install another GOG build under the game's settings.")
    summary = "With mods in Mod Organizer 2. Cyber Engine Tweaks and RED4ext load as they should."
    saves_note = "Saves sync through the game itself."
    setup_missing = "RootBuilder is missing, so CET and RED4ext won't load."

    def install_note(self) -> str:
        return "Set up for Cyberpunk, with RootBuilder for CET and RED4ext."

    def after_install(self, instance: Path, game, say) -> None:
        say("Adding the RootBuilder plugin (it puts CET's and RED4ext's DLLs next to the game while you play)…")
        install_rootbuilder(instance)

    def setup_finished(self, instance: Path) -> bool:
        return setup_finished(instance)


GAME = Cyberpunk(CYBERPUNK)

MODS_FOLDER = GAME.folder
RECORD_CONFLICTS = GAME.record_conflicts
Overview = mo2games.Overview


# --- the module as it was before the table ---------------------------------------


def with_dll_overrides(existing: str | None) -> str:
    """``WINEDLLOVERRIDES`` with winmm and version native-then-builtin, keeping
    every other override that was already set."""
    return _with_dll_overrides(existing, CYBERPUNK.dll_overrides)


def find_game():
    return GAME.find_game()


def chosen_instance() -> Path | None:
    return GAME.chosen_instance()


def find_instance() -> Path | None:
    return GAME.find_instance()


def instance_problem(path: Path) -> str | None:
    return GAME.instance_problem(path)


def use_instance(path: Path | str) -> Path:
    return GAME.use_instance(path)


def default_instance_dir() -> Path:
    return GAME.default_instance_dir()


def build_plan(instance_path, game, *, play: bool = False, download: str | None = None):
    return GAME.build_plan(instance_path, game, play=play, download=download)


def launch(launcher, *, play: bool) -> str:
    return GAME.launch(launcher, play=play)


def hook_status():
    return GAME.hook_status()


def enable_hook() -> list[str]:
    return GAME.enable_hook()


def disable_hook() -> str:
    return GAME.disable_hook()


def refresh_hook() -> list[str]:
    return GAME.refresh_hook()


def takes_downloads() -> bool:
    return GAME.takes_downloads()


def download(url: str, *, poll: float = 2.0) -> str:
    return GAME.download(url, poll=poll)


def install_mo2(dest, on_output=None, *, backend=None) -> Path:
    return GAME.install_mo2(dest, on_output, backend=backend)


def game_running() -> bool:
    return GAME.game_running()


def game_version(game=None) -> tuple[str, str] | None:
    return GAME.game_version(game)


def vault_versions(instance: Path | None = None) -> list[tuple[str, str]]:
    return GAME.vault_versions(instance)


def vault_version(instance: Path | None = None) -> tuple[str, str] | None:
    return GAME.vault_version(instance)


def version_problem(game=None, instance: Path | None = None) -> str | None:
    return GAME.version_problem(game, instance)


def overview(*, hook: bool = True) -> Overview:
    return GAME.overview(hook=hook)
