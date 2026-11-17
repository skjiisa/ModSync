"""A game ModSync runs Mod Organizer 2 for, whichever game and store it is.

Every game in :data:`modsync.games.MO2_GAMES` gets the same treatment from one
class, :class:`ManagedGame`: find it in Steam or Heroic, install Mod Organizer
2 for it with MO2-LINT (or take an instance set up some other way), open MO2
or play through it, make the store's own Play button play through MO2, send
its Nexus downloads to that MO2, and sync its mods with paired machines.
Saves are the store's business: the game stays the store's launch, so Steam
Cloud and GOG's or Epic's cloud saves work as they do without mods.

What differs between stores lives in two modules with the same functions,
:mod:`modsync.mo2games.steam` and :mod:`modsync.mo2games.heroic`. A game found
in a store is a plain record (``SteamGame``, ``heroic.library.HeroicGame``)
whose ``store`` says which of the two handles it.

What differs between games is data first (:data:`modsync.games.QUIRKS`: DLL
overrides, which of MO2's programs plays the game). A game that needs
something *done* subclasses :class:`ManagedGame` and is named in
:func:`_special`; Cyberpunk 2077 (:mod:`modsync.cyberpunk`) adds the
RootBuilder plugin that way.

Skyrim Special Edition on Steam and Daggerfall are not here: the vault, the
game version tools, SKSE and Daggerfall Unity are paths of their own.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import zipfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable

from modsync import background, config
from modsync.games import MO2_GAMES, STORES, Game
from modsync.mo2 import downloads, ini
from modsync.mo2.launch import LaunchPlan
from modsync.steam import pe
from modsync.sync import stignore
from modsync.sync.folders import FolderSpec

log = logging.getLogger(__name__)

OnOutput = Callable[[str], None]

# What MO2 calls each store in ModOrganizer.ini (game_edition), as MO2-LINT writes it.
EDITIONS = {"steam": "steam", "gog": "gog", "epic games": "epic", "epic": "epic"}
assert set(EDITIONS.values()) == set(STORES)

# MO2-LINT's own prefix steps for every game; the game's own (Game.tricks) follow.
BASE_TRICKS = ("win10", "arial", "fontsmooth=rgb")


class GameError(RuntimeError):
    pass


@dataclass
class Overview:
    """Everything the app shows about a game, read in one go (off the UI
    thread: it reads the store's files and the game's executable)."""

    game: object | None  # the game as its store has it: a SteamGame or a HeroicGame
    instance: Path | None  # the MO2 instance in use: the one chosen, else one MO2-LINT installed
    chosen: bool  # handed to ModSync (install or use): only that one syncs
    instance_problem: str | None
    hook: object | None  # the store's Play hook: its ``enabled`` says whether Play goes through MO2
    mods: str  # "on" | "off" | "stopping" | "kept" (a copy kept here for the other machines)
    version: tuple[str, str] | None  # the game here: (patch, build)
    version_problem: str | None
    prefix_ready: bool = False  # the store has made the game's prefix (read here, off the UI thread)
    needs_setup: bool = False  # the instance's setup stopped part way
    # The paired machines that sync the game's mods (by name): the ones that
    # offer them, and the ones a copy kept here is shared with.
    machines: list[str] = field(default_factory=list)
    kept_bytes: int | None = None  # how much the copy kept here takes
    kept_versions: list[tuple[str, str]] = field(default_factory=list)  # what the kept mods are for
    own_mods: bool = False  # the instance has mods installed: a kept copy can't simply move in

    @property
    def elsewhere(self) -> bool:
        """The game's mods sync on another machine: offered, or kept here for one."""
        return bool(self.machines) or self.mods == "kept"


def is_instance(path: Path) -> bool:
    return (path / "ModOrganizer.exe").is_file()


_SIZES: dict[Path, tuple[float, int]] = {}
_SIZE_TTL = 60.0  # a kept copy can hold tens of thousands of files: walked at most once a minute


def tree_bytes(root: Path) -> int | None:
    """How much a folder takes, None when it can't be read."""
    now = time.monotonic()
    cached = _SIZES.get(root)
    if cached is not None and now - cached[0] < _SIZE_TTL:
        return cached[1]
    if not root.is_dir():
        return None
    total = 0
    for top, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                pass
    _SIZES[root] = (now, total)
    return total


def host_which(name: str) -> str | None:
    """``name`` on the host's PATH, also from inside ModSync's Flatpak."""
    import shutil

    if not background.in_flatpak():
        return shutil.which(name)
    try:
        out = subprocess.run(["flatpak-spawn", "--host", "sh", "-c", f"command -v {name}"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def mo2_programs(sections: dict) -> list[dict[str, str]]:
    """MO2's executables (title, binary, arguments), in the order MO2 lists them."""
    entries: dict[int, dict[str, str]] = {}
    for key, value in sections.get("customExecutables", {}).items():
        index, _, field = key.partition("\\")
        if index.isdigit() and field.lower() in ("title", "binary", "arguments"):
            entries.setdefault(int(index), {})[field.lower()] = ini.unwrap_bytearray(value) or ""
    return [entries[i] for i in sorted(entries)]


def _program_name(entry: dict[str, str]) -> str:
    return PureWindowsPath(entry.get("binary", "").replace("/", "\\")).name.lower()


def _in_folder(binary: str, folder: Path) -> bool | None:
    """Whether MO2's program is in ``folder``; None when its drive letter
    isn't one ModSync can follow from here."""
    local = ini.wine_to_local(binary)
    if local is None:
        return None
    # /home is a link to /var/home on SteamOS, and either spelling may be MO2's.
    return os.path.realpath(local).lower().startswith(os.path.realpath(folder).lower() + os.sep)


class ManagedGame:
    """One game from the table, on this machine. Stateless but for what it
    reads: every method looks at the stores, MO2's files and ModSync's
    settings as they are now."""

    Error = GameError

    def __init__(self, game: Game) -> None:
        self.game = game
        stem, ext = self.vault_record.rsplit(".", 1)
        self.record_conflicts = f"{stem}.sync-conflict-*.{ext}"
        self.folder = self._folder_spec()

    # --- what a special case may change ------------------------------------------

    @property
    def key(self) -> str:
        return self.game.key

    @property
    def name(self) -> str:
        return self.game.name

    @property
    def short(self) -> str:
        """The name where a sentence wants a short one ("Cyberpunk's prefix")."""
        return self.game.name

    @property
    def slug(self) -> str:
        """Names ModSync's own files for the game: its settings, backups, launch record."""
        return self.game.key

    @property
    def ui_key(self) -> str:
        """The game in the app: its page, and what Home remembers showing."""
        return f"mo2-{self.game.key}"

    @property
    def command(self) -> str:
        return f"modsync games {self.game.key}"

    @property
    def settings_file(self) -> str:
        return f"games/{self.game.key}.json"

    @property
    def sync_key(self) -> str:
        return f"{self.game.key}-mo2"

    @property
    def launch_folders(self) -> tuple[str, ...]:
        """What Play and Open MO2 check first for changes still on their way,
        or files changed on two machines (ModSyncService.game_launch_check)."""
        return (self.folder.key,)

    vault_record = "modsync-game-version.json"  # which game version the synced mods are for; synced

    # Machine-local files playing leaves in MO2's overwrite, as .stignore
    # patterns. Before the content, since Syncthing takes the first match.
    leftovers: tuple[str, ...] = ("*.log",)

    version_advice = "Mods made for another version may not work."

    summary = "With mods in Mod Organizer 2."  # what the app says under the game's name
    saves_note = ""  # how saves sync, where it isn't the store's own cloud saves

    def install_note(self) -> str:
        return f"Set up for {self.short}."

    def after_install(self, instance: Path, game, say: OnOutput) -> None:
        """What the instance needs once MO2 and the prefix are ready. Each
        step does only what's missing: an install that stopped runs it again."""

    def setup_finished(self, instance: Path) -> bool:
        """Whether :meth:`after_install` got to its last step for this instance."""
        return True

    setup_missing = ""  # what an unfinished setup lacks, for the app

    # --- settings ---------------------------------------------------------------------

    def _settings_path(self) -> Path:
        """The game's own settings. Not in the state file: a ModSync from before
        this game would drop a field it doesn't know the next time it wrote that."""
        return config.config_dir() / self.settings_file

    def _settings(self) -> dict:
        try:
            data = json.loads(self._settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _update_settings(self, **changes) -> None:
        target = self._settings_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {**self._settings(), **changes}
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({k: v for k, v in data.items() if v is not None}, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    # --- the game in its store ------------------------------------------------------

    def _backend(self, game):
        from modsync.mo2games import heroic, steam

        return steam if game.store == "steam" else heroic

    def installs(self) -> list:
        """The game in every store that has it installed here, Steam first."""
        from modsync.heroic import library as heroic_library
        from modsync.mo2games import steam

        found: list = []
        if self.game.appid:
            in_steam = steam.find(self.game)
            if in_steam is not None:
                found.append(in_steam)
        for store in ("gog", "epic"):
            app_name = self.game.store_id(store)
            in_heroic = heroic_library.find_game(store, app_name) if app_name else None
            if in_heroic is not None:
                found.append(in_heroic)
        return found

    def find_game(self):
        """The game as its store installed it, or None. With the game in two
        stores, the one the MO2 instance was set up for."""
        found = self.installs()
        if len(found) < 2:
            return found[0] if found else None
        wanted = self._instance_store()
        return next((g for g in found if g.store == wanted), found[0])

    def _instance_store(self) -> str | None:
        """The store the instance in use was set up for, as MO2 records it."""
        instance = self.find_instance()
        if instance is None:
            return None
        general = ini.read_ini(instance / "ModOrganizer.ini").get("General", {})
        edition = (ini.unwrap_bytearray(ini.get_ci(general, "game_edition")) or "").strip().lower()
        return EDITIONS.get(edition)

    def where_to_get(self) -> str:
        """Where the game comes from, for a sentence about it missing."""
        places = [*(["Steam"] if self.game.appid else []),
                  *(["Heroic"] if self.game.gog_id or self.game.epic_id else [])]
        return " or ".join(places)

    def not_found(self, *, advice: bool = True) -> str:
        """That the game isn't installed, and (``advice``) what to do about it."""
        heroic_stores = " or ".join(n for n, i in (("GOG", self.game.gog_id), ("Epic", self.game.epic_id)) if i)
        if not self.game.appid:
            where, what = "Heroic", f"Install it from {heroic_stores} in Heroic first."
        elif not heroic_stores:
            where, what = "Steam", "Install it and launch it once through Steam first."
        else:
            where, what = "Steam or Heroic", "Install it in one of them first."
        return f"{self.name} wasn't found in {where}." + (f" {what}" if advice else "")

    # --- the MO2 instance ---------------------------------------------------------

    def _mo2lint_instances(self) -> list[Path]:
        """The game's instances MO2-LINT installed, from its state file."""
        from modsync.mo2.installers.mo2lint import mo2lint_state_path

        try:
            data = json.loads(mo2lint_state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = data.get("instances") if isinstance(data, dict) else None
        return [Path(e["instance_path"]) for e in entries or []
                if isinstance(e, dict) and e.get("game") == self.game.mo2lint_key
                and isinstance(e.get("instance_path"), str)]

    def chosen_instance(self) -> Path | None:
        """The instance ``use`` or ``install`` chose, whether or not it's there now."""
        chosen = self._settings().get("instance")
        return Path(chosen) if isinstance(chosen, str) and chosen else None

    def find_instance(self) -> Path | None:
        """The instance chosen, else one MO2-LINT installed. A chosen one
        that's missing (an unmounted drive) is still the answer, so a launch
        says so instead of starting a different instance's mods."""
        return self.chosen_instance() or next((p for p in self._mo2lint_instances() if is_instance(p)), None)

    def instance_problem(self, path: Path) -> str | None:
        """Why this folder can't be the game's MO2 instance, or None."""
        if not is_instance(path):
            return f"{path} has no ModOrganizer.exe."
        general = ini.read_ini(path / "ModOrganizer.ini").get("General", {})
        name = ini.unwrap_bytearray(ini.get_ci(general, "gameName"))
        if name and not self.game.is_mo2_name(name):
            return f"That MO2 instance manages {name}, not {self.name}."
        return None

    def use_instance(self, path: Path | str) -> Path:
        instance = Path(path).expanduser().resolve()
        problem = self.instance_problem(instance)
        if problem:
            raise self.Error(problem)
        self._update_settings(instance=str(instance))
        return instance

    def default_instance_dir(self) -> Path:
        return Path.home() / f"ModOrganizer2-{self.game.mo2lint_key}"

    # --- launching ----------------------------------------------------------------

    def program(self, game_dir: Path, store: str) -> Path:
        """The game's own program in ``game_dir``. MO2-LINT names Baldur's
        Gate 3's without its extension (``bin/bg3``, the Linux build's name)."""
        exe = game_dir / self.game.exe_for(store)
        if not exe.suffix and not exe.is_file() and exe.with_name(exe.name + ".exe").is_file():
            return exe.with_name(exe.name + ".exe")
        return exe

    def direct_args(self, game_dir: Path, store: str) -> list[str]:
        """MO2 running the game's own program, for an MO2 that hasn't listed
        its programs yet."""
        exe = self.program(game_dir, store)
        arguments = ["-a", self.game.direct_arguments] if self.game.direct_arguments else []
        return ["run", *arguments, "-c", "Z:" + str(exe.parent), "Z:" + str(exe)]

    def play_target(self, sections: dict, game_dir: Path, store: str) -> tuple[list[str], str]:
        """MO2's arguments to play the game, and what it's called: one of the
        programs MO2 lists (Game.play_exes; else the first one from the
        game's folder, so MO2's own order decides between a script extender,
        the game and its launcher), else the game's own program.
        ``sections`` are MO2's settings and ``game_dir`` the game's folder."""
        programs = [e for e in mo2_programs(sections) if e.get("title") and e.get("binary")]
        wanted = [name.lower() for name in self.game.play_exes]
        if wanted:
            found = sorted((e for e in programs if _program_name(e) in wanted),
                           key=lambda e: wanted.index(_program_name(e)))
        else:
            found = [e for e in programs if _in_folder(e["binary"], game_dir)
                     and ini.wine_to_local(e["binary"]).is_file()]
        if self.game.play_argument:
            found.sort(key=lambda e: self.game.play_argument not in e.get("arguments", ""))
        if found:
            return ["run", "-e", found[0]["title"]], found[0]["title"]
        exe = self.program(game_dir, store)
        if not exe.is_file():
            raise self.Error(f"{exe} is missing. Check the game's files in "
                             f"{'Steam' if store == 'steam' else 'Heroic'}.")
        return self.direct_args(game_dir, store), self.name

    def build_plan(self, instance_path: Path | str | None, game, *, play: bool = False,
                   download: str | None = None) -> LaunchPlan:
        """MO2's launch in the game's own prefix: open, playing the game
        (``play``), or open with an nxm:// link MO2 downloads (``download``)."""
        if not instance_path:
            raise self.Error(f"Choose {self.short}'s MO2 instance first ('{self.command} use <dir>').")
        instance = Path(instance_path).expanduser().resolve()
        problem = self.instance_problem(instance)
        if problem:
            raise self.Error(problem)
        if game is None:
            raise self.Error(self.not_found())
        backend = self._backend(game)
        waiting = backend.not_ready(self, game)
        if waiting:
            raise self.Error(waiting)
        if play:
            general = ini.read_ini(instance / "ModOrganizer.ini").get("General", {})
            if not ini.unwrap_bytearray(ini.get_ci(general, "gameName")):
                raise self.Error("Open MO2 and finish setting up the instance before playing.")
        return backend.build_plan(self, instance, game, play=play, download=download)

    def launch(self, launcher, *, play: bool) -> str:
        """Open MO2, or play through it, from the app or the command line.
        While the store's Play hook is on, the store launches the game as its
        own Play button does and the hook does the rest, so the session is
        the store's: it syncs cloud saves and counts the playtime. Otherwise
        ModSync starts MO2 itself, with the game's prefix and Proton."""
        self.refresh_hook()
        game, instance = self.find_game(), self.find_instance()
        plan = self.build_plan(instance, game, play=play)  # says what's missing, either way
        backend = self._backend(game)
        if not backend.through_hook(self, game, plan.cwd):
            backend.prepare(self, game, plan)
            return launcher.start(plan)
        target = backend.downloads_target(self, game, plan.cwd)
        with downloads.launch_lock(target.record) as record:
            if target.running() or downloads.under_way(record, target.note, target.taken):
                raise self.Error(f"Mod Organizer 2 is already open for {self.name}."
                                 + (" Play from there, or close it first." if play else ""))
            with downloads.launched(record, "hook"):
                backend.ask_store(self, game, downloads.PLAY if play else downloads.OPEN)
        through = backend.LAUNCHER
        return f"Starting {self.name} through {through}…" if play else f"Opening Mod Organizer 2 through {through}…"

    # --- the store's Play button ------------------------------------------------------

    def hook_status(self):
        game, instance = self.find_game(), self.find_instance()
        if game is None:
            return None
        usable = instance if instance is not None and is_instance(instance) else None
        return self._backend(game).hook_status(self, game, usable)

    def enable_hook(self) -> list[str]:
        game, instance = self.find_game(), self.find_instance()
        if game is None:
            raise self.Error(self.not_found())
        if instance is None:
            raise self.Error(f"Install or choose {self.short}'s MO2 instance first ('{self.command} install').")
        problem = self.instance_problem(instance)
        if problem:
            raise self.Error(problem)
        done = self._backend(game).enable_hook(self, game, instance.resolve())
        from modsync import nxm

        note = nxm.claim_links()
        return [*done, note] if note else done

    def disable_hook(self) -> str:
        game = self.find_game()
        if game is None:
            raise self.Error(self.not_found(advice=False))
        return self._backend(game).disable_hook(self, game)

    def refresh_hook(self) -> list[str]:
        """Keep an enabled hook in step with the instance and the game's
        folder, and with this ModSync's script. Run when ModSync starts and
        whenever the instance may have changed. Never raises; returns what
        the user should know."""
        try:
            game, instance = self.find_game(), self.find_instance()
            if game is None or instance is None or self.instance_problem(instance):
                return []
            return self._backend(game).refresh_hook(self, game, instance.resolve())
        except Exception as e:  # noqa: BLE001 — a refresh must never stop what called it
            log.warning("couldn't refresh the Play hook for %s: %s", self.name, e)
            return []

    # --- Nexus downloads (nxm:// links, see modsync.nxm) ----------------------------

    def takes_downloads(self) -> bool:
        """Whether the game's nxm:// links are ModSync's to open: the game is
        installed and there's an MO2 instance for it."""
        instance = self.find_instance()
        return self.find_game() is not None and instance is not None and self.instance_problem(instance) is None

    def download(self, url: str, *, poll: float = 2.0, on_wait: Callable[[str], None] | None = None) -> str:
        """Hand a Nexus download to the game's MO2. An open MO2 gets it from a
        second ModOrganizer.exe, which passes its link to the first and exits.
        A closed one is opened with the link: through the store while its Play
        hook is on, so the session is the store's, else by ModSync directly.
        A link that comes while MO2 is still starting for an earlier one waits
        for it (see modsync.mo2.downloads). Returns what happened."""
        game, instance = self.find_game(), self.find_instance()
        if game is None:
            raise self.Error(self.not_found(advice=False))
        if instance is None:
            raise self.Error(f"{self.name} has no Mod Organizer 2 instance ('{self.command} install').")
        problem = self.instance_problem(instance)
        if problem:
            raise self.Error(problem)
        backend = self._backend(game)
        waiting = backend.not_ready(self, game)
        if waiting:
            raise self.Error(waiting)
        return backend.download(self, game, instance, url, poll=poll, on_wait=on_wait)

    def launch_record(self) -> Path:
        """When ModSync last started MO2, for a download or its own Play or
        Open MO2 (its time is the file's), and how: "hook" or "direct"."""
        return downloads.shared_dir() / f"{self.slug}-download-launch"

    # --- Steam's launch (modsync.launchhook, 'modsync launch hub') --------------------

    def steam_instance(self) -> Path | None:
        """The instance, while the game in use is Steam's: the one Steam's
        launches and nxm:// links go to."""
        game = self.find_game()
        return self.find_instance() if game is not None and game.store == "steam" else None

    def steam_plan(self, *, play: bool = False, download: str | None = None, prepare: bool = False) -> LaunchPlan:
        """MO2's launch in Steam's prefix for the game. ``prepare``: also
        everything Open MO2 does before MO2 starts."""
        game = self.find_game()
        if game is None or game.store != "steam":
            raise self.Error(f"{self.name} wasn't found in Steam.")
        plan = self.build_plan(self.find_instance(), game, play=play, download=download)
        if prepare:
            self._backend(game).prepare(self, game, plan)
        return plan

    # --- installing MO2 -----------------------------------------------------------

    def game_running(self) -> bool:
        """The game or MO2 running in Wine. A running session holds the
        prefix. Wine names its processes by their Windows path
        (``Z:\\…\\Game.exe``), which a shell command merely mentioning the
        program doesn't start with."""
        names = {Path(exe).stem for exe in (self.game.exe_name, *dict(self.game.store_exes).values(),
                                           *self.game.play_exes) if exe}
        pattern = "|".join(sorted(re.escape(n) for n in (*names, "ModOrganizer")))
        try:
            found = background.run_host(["pgrep", "-f", rf"^[A-Za-z]:\\.*\\({pattern})\.exe"], timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return found.returncode == 0

    def _ready_game(self):
        game = self.find_game()
        if game is None:
            raise self.Error(self.not_found())
        backend = self._backend(game)
        waiting = backend.not_ready(self, game) or backend.install_blocked(self, game)
        if waiting:
            raise self.Error(waiting)
        if self.game_running():
            raise self.Error(f"Close {self.name} and Mod Organizer 2 first.")
        return game

    def install_mo2(self, dest: Path | str, on_output: OnOutput | None = None, *, backend=None) -> Path:
        """An MO2 instance for the game in ``dest``: MO2-LINT's install (MO2,
        its config and the store's way of opening it), the prefix steps, and
        whatever the game needs on top (:meth:`after_install`). Then it's the
        instance in use. An instance for the game already in ``dest`` (an
        install a later step stopped, or one to repair) skips MO2-LINT and
        gets the rest, each step doing only what's missing."""
        from modsync.mo2.installers import Mo2LintBackend

        say = on_output or (lambda _line: None)
        game = self._ready_game()
        store = self._backend(game)
        instance = Path(dest).expanduser()
        if is_instance(instance):
            problem = self.instance_problem(instance)
            if problem:
                raise self.Error(problem)
            say(f"MO2 is already in {instance}; finishing its setup.")
        else:
            backend = backend or Mo2LintBackend()
            ok, reason = backend.available()
            if not ok:
                raise self.Error(f"Cannot install: {reason}")
            result = store.mo2lint_install(self, game, backend, instance, on_output)
            if not result.success or result.instance_path is None:
                raise self.Error(f"Install failed: {result.message}")
            instance = result.instance_path
        try:
            store.prepare_prefix(self, game, on_output)
            self.after_install(instance, game, say)
        except (GameError, OSError, zipfile.BadZipFile) as e:
            raise self.Error(f"{e} MO2 is installed in {instance}; run the install again to finish its setup.") from e
        chosen = self.use_instance(instance)
        for note in self.refresh_hook():
            say(note)
        return chosen

    # --- syncing the mods -----------------------------------------------------------

    def _synced_instance(self) -> Path | None:
        """The instance handed to ModSync, never one it only found: syncing
        that would change a folder nobody gave it."""
        chosen = self.chosen_instance()
        return chosen if chosen is not None and self.instance_problem(chosen) is None else None

    def kept_root(self) -> Path | None:
        """Where a copy of the game's mods is kept here for the other machines
        (see modsync.sync.folders.hold), or None."""
        from modsync.state import State

        kept = State.load().held_folders.get(self.folder.key)
        return Path(kept) if kept else None

    def _records_root(self) -> Path | None:
        """Where the synced version records are: the instance that syncs,
        else the copy kept here, which holds the same files."""
        return self._synced_instance() or self.kept_root()

    def version_exe(self, game) -> Path | None:
        """The program whose version is the game's. A launcher's isn't."""
        exe = self.game.exe_for(game.store)
        if not exe or "launcher" in Path(exe).name.lower():
            return None
        return self.program(game.install_path, game.store)

    def game_version(self, game=None) -> tuple[str, str] | None:
        """The game here as (patch, build): ``("2.5", "3.0.88.46235")``."""
        game = game or self.find_game()
        exe = self.version_exe(game) if game is not None else None
        if exe is None:
            return None
        build, product = pe.build_and_product(exe)
        if build is None:
            return None
        return product or ".".join(map(str, build)), ".".join(map(str, build))

    def _record_vault_version(self, root: Path) -> None:
        """The machine that starts syncing the mods says which game version
        they're for. When it can't read the game's version yet, it remembers
        that it owes the record (:meth:`_owed_record`); a machine that joins
        never writes one."""
        record = root / self.vault_record
        if record.exists():
            return
        game = self.find_game()
        if game is not None and self.version_exe(game) is None:
            return  # nothing here says which version the game is
        version = self.game_version(game)
        if version is None:
            self._update_settings(record_owed=True)
            return
        record.write_text(json.dumps({"game_version": version[0], "build": version[1]}, indent=2) + "\n",
                          encoding="utf-8")
        self._update_settings(record_owed=None)

    def _owed_record(self, root: Path) -> None:
        """Run whenever the folder's status is read: write a record owed since sharing."""
        if self._settings().get("record_owed"):
            self._record_vault_version(root)

    @staticmethod
    def _read_record(path: Path) -> tuple[str, str] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("build"), str):
            return None
        return str(data.get("game_version") or data["build"]), data["build"]

    def vault_versions(self, instance: Path | None = None) -> list[tuple[str, str]]:
        """Every version the synced records name: two machines that each
        started sharing before they met leave a conflict copy of the record too."""
        instance = instance or self._records_root()
        if instance is None:
            return []
        paths = [instance / self.vault_record, *sorted(instance.glob(self.record_conflicts))]
        return list(dict.fromkeys(v for p in paths if (v := self._read_record(p))))

    def vault_version(self, instance: Path | None = None) -> tuple[str, str] | None:
        instance = instance or self._records_root()
        return self._read_record(instance / self.vault_record) if instance else None

    def version_problem(self, game=None, instance: Path | None = None) -> str | None:
        """A note when the game here isn't the version the synced mods are
        for. Only a note: whether to change versions is the player's call."""
        here = self.game_version(game)
        others = [v for v in self.vault_versions(instance) if here is not None and v[1] != here[1]]
        if not others:
            return None
        wanted = " or ".join(f"{v[0]} (build {v[1]})" for v in others)
        return (f"{self.name} here is {here[0]} (build {here[1]}), but the synced mods were set up for "
                f"{wanted}. {self.version_advice}")

    @staticmethod
    def _accepts_offer(root: Path) -> bool:
        """A paired machine's mods fill an instance by themselves only while
        it has none installed: merging two setups (both machines' mods enabled
        in each other's profiles) is for 'modsync sync share' to choose."""
        try:
            return not any((root / "mods").iterdir())
        except OSError:
            return True  # no mods folder: nothing to merge with

    def _folder_spec(self) -> FolderSpec:
        return FolderSpec(
            key=self.sync_key,
            label=f"{self.name} mods (ModSync)",
            path=lambda: self._synced_instance(),
            ignore=(
                f"// Managed by ModSync. Syncs {self.name}'s Mod Organizer 2 content: mods, profiles,",
                "// downloads and overwrite. Never the MO2 program, ModOrganizer.ini, nexusApiKey,",
                "// or what playing leaves behind.",
                "",
                *self.leftovers,
                "",
                *(f"!/{name}" for name in stignore.CONTENT_DIRS),
                "!/categories.dat",
                f"!/{self.vault_record}",
                f"!/{self.record_conflicts}",
                "",
                "// Everything else at the instance root. Ignoring a folder skips what's in it.",
                "/*",
            ),
            content=(*stignore.CONTENT_DIRS, "categories.dat", self.vault_record),
            on_share=lambda root: self._record_vault_version(root),
            in_use=lambda: self.game_running(),
            tidy=lambda root: self._owed_record(root),
            accepts=lambda root: self._accepts_offer(root),
        )

    # --- for the app ------------------------------------------------------------------

    def overview(self, *, hook: bool = True, machines: list[str] | None = None) -> Overview:
        """``hook``: also read the Play hook (the store's settings for the
        game). ``machines``: the paired machines that sync the game's mods,
        from ModSyncService.games_elsewhere (only Syncthing knows them)."""
        from modsync.state import State
        from modsync.sync import folders

        game, instance = self.find_game(), self.find_instance()
        st = State.load()
        key = self.folder.key
        kept = st.held_folders.get(key)
        if folders.stopping(key, st):
            mods = "stopping"
        else:
            mods = "on" if key in st.game_folders else "kept" if kept else "off"
        status = None
        if hook and game is not None:
            try:
                status = self.hook_status()
            except Exception as exc:  # the store's files unreadable: show the rest
                log.warning("could not read the Play hook for %s: %s", self.name, exc)
        chosen = instance is not None and instance == self.chosen_instance()
        problem = self.instance_problem(instance) if instance else None
        ready = game is not None and self._backend(game).not_ready(self, game) is None
        return Overview(game, instance, chosen, problem, status, mods,
                        self.game_version(game) if game else None, self.version_problem(game) if game else None,
                        prefix_ready=ready,
                        needs_setup=instance is not None and problem is None and not self.setup_finished(instance),
                        machines=list(machines or ()),
                        kept_bytes=tree_bytes(Path(kept)) if kept else None,
                        kept_versions=self.vault_versions(Path(kept)) if kept else [],
                        own_mods=instance is not None and problem is None and not self._accepts_offer(instance))


# --- the games -----------------------------------------------------------------------

_registry: dict[str, ManagedGame] | None = None
_registry_lock = threading.Lock()  # the app asks from its worker threads too


def _special() -> list[ManagedGame]:
    """The games that need more than the table says: each a subclass's instance."""
    from modsync import cyberpunk

    return [cyberpunk.GAME]


def all_games() -> dict[str, ManagedGame]:
    """Every game ModSync runs MO2 for this way, by key (MO2-LINT's key for it)."""
    global _registry
    with _registry_lock:
        if _registry is None:
            special = {game.key: game for game in _special()}
            _registry = {key: special.get(key) or ManagedGame(game) for key, game in MO2_GAMES.items()}
    return _registry


def get(key: str) -> ManagedGame | None:
    return all_games().get(key)


def for_steam(appid: int | None) -> ManagedGame | None:
    """The game with this Steam id."""
    return next((g for g in all_games().values() if appid and g.game.appid == appid), None)


def for_ui(ui_key: str) -> ManagedGame | None:
    return next((g for g in all_games().values() if g.ui_key == ui_key), None)


def for_folder(sync_key: str) -> ManagedGame | None:
    """The game whose mods the synced folder ``sync_key`` holds."""
    return next((g for g in all_games().values() if g.folder.key == sync_key), None)


def for_nexus(slug: str) -> list[ManagedGame]:
    """The games Nexus calls ``slug``: editions of a game share one."""
    return [g for g in all_games().values() if g.game.nexus_slug == slug]


def listed(elsewhere: dict[str, list[str]] | None = None) -> list[ManagedGame]:
    """The games the app's switcher lists: set up here (an MO2 instance for
    them, chosen or MO2-LINT's), kept here for other machines, or synced on
    a paired machine (``elsewhere``, by synced folder). The others are a
    choice under "Add a game…"."""
    from modsync.state import State

    st = State.load()
    elsewhere = elsewhere or {}
    return [game for game in all_games().values()
            if game.folder.key in st.held_folders or elsewhere.get(game.folder.key)
            or game.find_instance() is not None]


def here() -> list[ManagedGame]:
    """The games on this machine: installed in Steam or Heroic, or with an MO2
    instance handed to ModSync. Steam's libraries and Heroic's files are read
    once for all of them."""
    from modsync.heroic import library as heroic_library
    from modsync.mo2games import steam

    in_steam = steam.installed_ids()
    in_heroic = {(g.store, g.app_name) for g in heroic_library.installed()}
    return [game for game in all_games().values()
            if game.game.appid in in_steam
            or any((store, game.game.store_id(store)) in in_heroic for store in ("gog", "epic"))
            or game.chosen_instance() is not None]


def refresh_hooks() -> list[str]:
    """Keep every game's Play hook current (see ManagedGame.refresh_hook).
    Never raises; returns what the user should know."""
    notes: list[str] = []
    try:
        games = here()
    except Exception as e:  # noqa: BLE001
        log.warning("couldn't look for the games here: %s", e)
        return notes
    for game in games:
        notes += game.refresh_hook()
    return notes
