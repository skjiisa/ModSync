"""Cyberpunk 2077 from GOG, installed and run by the Heroic Games Launcher,
modded with Mod Organizer 2.

Heroic runs the game with umu-launcher and the Proton chosen in its settings,
in a prefix of its own (:mod:`modsync.heroic.library`). MO2 runs the same way:
``umu-run ModOrganizer.exe`` with Heroic's prefix and Proton, so the game MO2
starts finds the prefix exactly as Heroic left it. ``GAMEID`` is the game's
Steam id with umu's prefix, as Heroic sets it, so umu's protonfixes for the
game apply.

MO2's Cyberpunk plugin (MO2's basic_games) knows the game: its default
executable runs ``Cyberpunk2077.exe --launcher-skip -modded``, skipping
REDlauncher and deploying REDmod first. Cyber Engine Tweaks and RED4ext are
loaded through proxy DLLs (``version.dll``, ``winmm.dll``) next to the game,
which Wine ignores in favour of its own unless told otherwise, so every launch
sets :data:`DLL_OVERRIDES`. ``n,b`` falls back to Wine's own DLL when there is
no mod's copy, so it is harmless without mods. Those DLLs reach the game folder
through the RootBuilder plugin, which MO2 doesn't ship: :func:`install_mo2`
adds it, set up the way the Cyberpunk plugin would set it on MO2's first start
(copy mode: the files are copied in for the session and taken out again when
the game exits).

MO2-LINT installs MO2 itself, but on GOG its prefix steps run winetricks with
whatever ``wine`` is on PATH (a host Wine on a desktop, which upgrades the
Proton prefix and stops at a Wine Mono dialog; none on a Steam Deck) and pass
the VC++ installer and DLL overrides to winetricks as verbs, which fail.
:data:`MO2LINT_ENV` makes each of those steps fail at once without touching the
prefix, and :func:`prepare_prefix` does them with the game's own runner.

:data:`MODS_FOLDER` syncs the instance's mods, profiles and downloads with
paired machines (see :mod:`modsync.sync.folders`), under the same rules as
Skyrim's vault, plus what the game, CET, RED4ext and vkd3d write into
``overwrite`` while playing. Only an instance handed to ModSync (``install`` or
``use``) is ever synced. Saves don't: the game syncs them across stores itself.
The machine that starts syncing records the game version the mods are for, and
a different version here is reported, never acted on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Callable

from modsync import background, config
from modsync.games import CYBERPUNK
from modsync.heroic import hook as heroic_hook
from modsync.heroic import library as heroic
from modsync.logging_setup import state_dir
from modsync.mo2 import ini
from modsync.mo2.launch import LaunchPlan, nested_desktop_display
from modsync.steam import pe
from modsync.sync import stignore
from modsync.sync.folders import FolderSpec

log = logging.getLogger(__name__)

DLL_OVERRIDES = "winmm,version=n,b"
_OVERRIDDEN = {"winmm", "version"}

MO2LINT_ENV = {"WINE": "/bin/false", "WINELOADER": "/bin/false", "WINESERVER": "/bin/true"}
# MO2-LINT's own prefix steps for Cyberpunk, less vcrun2022: winetricks' vcrun2022
# can leave a broken runtime (MO2-LINT #1106), so prepare_prefix runs Microsoft's
# redistributable instead, as ModSync does for Skyrim.
TRICKS = ("win10", "arial", "fontsmooth=rgb", "xaudio2_7=native", "d3dcompiler_47")

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

OnOutput = Callable[[str], None]


class CyberpunkError(RuntimeError):
    pass


def find_game() -> heroic.GogGame | None:
    """Cyberpunk as Heroic installed it, or None."""
    return heroic.find_gog_game(CYBERPUNK.gog_id)


def with_dll_overrides(existing: str | None) -> str:
    """``WINEDLLOVERRIDES`` with winmm and version native-then-builtin, keeping
    every other override that was already set."""
    kept: list[str] = []
    for entry in (existing or "").split(";"):
        dlls, sep, mode = entry.partition("=")
        names = [d for d in dlls.split(",") if d.strip() and d.strip().lower() not in _OVERRIDDEN]
        if names:
            kept.append(",".join(names) + sep + mode)
    return ";".join([*kept, DLL_OVERRIDES])


# --- the MO2 instance ---------------------------------------------------------


def _is_instance(path: Path) -> bool:
    return (path / "ModOrganizer.exe").is_file()


def _mo2lint_instances() -> list[Path]:
    """Cyberpunk instances MO2-LINT installed, from its state file."""
    from modsync.mo2.installers.mo2lint import mo2lint_state_path

    try:
        data = json.loads(mo2lint_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("instances") if isinstance(data, dict) else None
    return [Path(e["instance_path"]) for e in entries or []
            if isinstance(e, dict) and e.get("game") == CYBERPUNK.mo2lint_key and isinstance(e.get("instance_path"), str)]


def _settings_path() -> Path:
    """Cyberpunk's own settings. Not in the state file: a ModSync from before
    Cyberpunk would drop a field it doesn't know the next time it wrote that."""
    return config.config_dir() / "cyberpunk.json"


def _settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_settings(**changes) -> None:
    target = _settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {**_settings(), **changes}
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({k: v for k, v in data.items() if v is not None}, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def chosen_instance() -> Path | None:
    """The instance ``modsync cyberpunk use`` chose, whether or not it's there now."""
    chosen = _settings().get("instance")
    return Path(chosen) if isinstance(chosen, str) and chosen else None


def find_instance() -> Path | None:
    """The instance ``modsync cyberpunk use`` chose, else one MO2-LINT installed.
    A chosen one that's missing (an unmounted drive) is still the answer, so a
    launch says so instead of starting a different instance's mods."""
    return chosen_instance() or next((p for p in _mo2lint_instances() if _is_instance(p)), None)


def instance_problem(path: Path) -> str | None:
    """Why this folder can't be Cyberpunk's MO2 instance, or None."""
    if not _is_instance(path):
        return f"{path} has no ModOrganizer.exe."
    general = ini.read_ini(path / "ModOrganizer.ini").get("General", {})
    name = ini.unwrap_bytearray(ini.get_ci(general, "gameName"))
    if name and name != CYBERPUNK.mo2_game_name:
        return f"That MO2 instance manages {name}, not {CYBERPUNK.name}."
    return None


def use_instance(path: Path | str) -> Path:
    instance = Path(path).expanduser().resolve()
    problem = instance_problem(instance)
    if problem:
        raise CyberpunkError(problem)
    _update_settings(instance=str(instance))
    return instance


# --- launching ----------------------------------------------------------------


def _host_which(name: str) -> str | None:
    """``name`` on the host's PATH, also from inside ModSync's Flatpak."""
    if not background.in_flatpak():
        return shutil.which(name)
    try:
        out = subprocess.run(["flatpak-spawn", "--host", "sh", "-c", f"command -v {name}"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def runner(game: heroic.GogGame) -> list[str]:
    """What runs a Windows program in the game's prefix: umu-launcher for a
    Proton (the system's, else the copy Heroic downloads), the Wine binary
    itself for a plain Wine."""
    if game.wine is None:
        raise CyberpunkError("Heroic has no Wine or Proton set for Cyberpunk. Pick one in its settings.")
    if not game.wine.is_proton:
        return [str(game.wine.bin)]
    system = _host_which("umu-run")
    if system:
        return [system]
    bundled = game.config_dir / "tools" / "runtimes" / "umu" / "umu_run.py"
    if bundled.is_file():
        return ["python3", str(bundled)]
    raise CyberpunkError("umu-launcher wasn't found. Launch Cyberpunk once from Heroic, which downloads it.")


def _game_target(sections: dict, game_dir: Path) -> tuple[list[str], str]:
    """MO2's arguments to start the game: the plugin's default executable
    (REDmod deploy, then the game) when MO2 has saved it, else the game itself
    with the same arguments."""
    entries: dict[str, dict[str, str]] = {}
    for key, value in sections.get("customExecutables", {}).items():
        index, _, field = key.partition("\\")
        if index.isdigit() and field in ("title", "binary", "arguments"):
            entries.setdefault(index, {})[field] = ini.unwrap_bytearray(value) or ""
    games = [e for e in entries.values()
             if e.get("title") and PureWindowsPath(e.get("binary", "")).name.lower() == "cyberpunk2077.exe"]
    games.sort(key=lambda e: "-modded" not in e.get("arguments", ""))
    if games:
        return ["run", "-e", games[0]["title"]], games[0]["title"]
    exe = game_dir / CYBERPUNK.exe_name
    if not exe.is_file():
        raise CyberpunkError(f"{exe} is missing. Check the game's files in Heroic.")
    return _direct_args(game_dir), CYBERPUNK.name


def _direct_args(game_dir: Path) -> list[str]:
    """MO2 running the game itself, with the arguments of the Cyberpunk plugin's
    executable, for an MO2 that hasn't saved its executables yet."""
    exe = game_dir / CYBERPUNK.exe_name
    return ["run", "-a", "--launcher-skip -modded", "-c", "Z:" + str(exe.parent), "Z:" + str(exe)]


def build_plan(instance_path: Path | str | None, game: heroic.GogGame | None, *, play: bool = False,
               download: str | None = None) -> LaunchPlan:
    """``download``: an nxm:// link MO2 downloads once it's open."""
    if not instance_path:
        raise CyberpunkError("Choose Cyberpunk's MO2 instance first ('modsync cyberpunk use <dir>').")
    instance = Path(instance_path).expanduser().resolve()
    problem = instance_problem(instance)
    if problem:
        raise CyberpunkError(problem)
    if game is None:
        raise CyberpunkError("Cyberpunk 2077 wasn't found in Heroic. Install it from GOG in Heroic first.")
    if game.drive_c is None:
        raise CyberpunkError("Cyberpunk's prefix isn't set up yet. Launch the game once from Heroic first.")
    command_runner = runner(game)

    sections = ini.read_ini(instance / "ModOrganizer.ini")
    general = sections.get("General", {})
    mo2_args: list[str] = []
    profile = ini.unwrap_bytearray(ini.get_ci(general, "selected_profile"))
    if profile:
        mo2_args += ["-p", profile]
    target = "Mod Organizer 2"
    if play:
        if not ini.unwrap_bytearray(ini.get_ci(general, "gameName")):
            raise CyberpunkError("Open MO2 and finish setting up the instance before playing.")
        args, target = _game_target(sections, game.install_path)
        mo2_args += args
    elif download:
        mo2_args.append(download)
    command = [str(instance / "ModOrganizer.exe"), *mo2_args]
    env = prefix_env(game)
    display = nested_desktop_display()
    if display:
        env.update({"DISPLAY": display, "ENABLE_GAMESCOPE_WSI": "0", "DISABLE_GAMESCOPE_WSI": "1"})
    return LaunchPlan([*command_runner, *command], env, instance, target, play, game.wine.proton_dir, command)


def prefix_env(game: heroic.GogGame) -> dict[str, str]:
    """What :func:`runner` needs to run in the game's prefix, as Heroic sets it."""
    env = {
        "WINEPREFIX": str(game.prefix),
        "WINEDLLOVERRIDES": with_dll_overrides(os.environ.get("WINEDLLOVERRIDES")),
    }
    proton = game.wine.proton_dir if game.wine is not None else None
    if proton is not None:
        env.update({
            "STEAM_COMPAT_DATA_PATH": str(game.prefix),
            "STEAM_COMPAT_INSTALL_PATH": str(game.install_path),
            "PROTONPATH": str(proton),
            "GAMEID": f"umu-{CYBERPUNK.appid}",
            "STORE": "gog",
        })
    return env


# --- Heroic's Play button -----------------------------------------------------


def hook_script(game: heroic.GogGame, instance: Path) -> str:
    """The Play hook as it should be now. Like Play, it runs MO2's executable
    that deploys REDmod; it passes no profile, so MO2 plays whichever profile
    was last chosen in it."""
    return heroic_hook.render(game, game_name=CYBERPUNK.name, instance=instance.resolve(),
                              game_exe=PureWindowsPath(CYBERPUNK.exe_name).name, prefer_argument="-modded",
                              direct_args=_direct_args(game.install_path),
                              disable_command="modsync cyberpunk hook disable")


def hook_status() -> heroic_hook.HookStatus | None:
    game, instance = find_game(), find_instance()
    if game is None:
        return None
    expected = hook_script(game, instance) if instance is not None and _is_instance(instance) else None
    return heroic_hook.status(game, expected)


def enable_hook() -> list[str]:
    game, instance = find_game(), find_instance()
    if game is None:
        raise CyberpunkError("Cyberpunk 2077 wasn't found in Heroic. Install it from GOG in Heroic first.")
    if instance is None:
        raise CyberpunkError("Install or choose Cyberpunk's MO2 instance first ('modsync cyberpunk install').")
    problem = instance_problem(instance)
    if problem:
        raise CyberpunkError(problem)
    try:
        done = heroic_hook.enable(game, hook_script(game, instance), instance.resolve())
    except heroic_hook.HookError as e:
        raise CyberpunkError(str(e)) from e
    # MO2-LINT's handler for Nexus downloads would play the game through the
    # hook rather than open MO2, so ModSync takes the links (modsync.nxm).
    from modsync import nxm

    if not nxm.status().active:
        try:
            done.append(nxm.enable())
        except (OSError, nxm.NxmError) as e:
            done.append(f"Nexus Mods downloads couldn't be pointed at ModSync ({e}); 'modsync nxm enable' retries.")
    return done


def disable_hook() -> str:
    game = find_game()
    if game is None:
        raise CyberpunkError("Cyberpunk 2077 wasn't found in Heroic.")
    try:
        return heroic_hook.disable(game)
    except heroic_hook.HookError as e:
        raise CyberpunkError(str(e)) from e


def refresh_hook() -> list[str]:
    """Keep an enabled hook in step with the instance and the game's folder,
    and with this ModSync's script. Run when ModSync starts and whenever the
    instance may have changed. Never raises; returns what the user should know."""
    try:
        game, instance = find_game(), find_instance()
        if game is None or instance is None or instance_problem(instance):
            return []
        return heroic_hook.refresh(game, hook_script(game, instance), instance.resolve())
    except (OSError, CyberpunkError, heroic_hook.HookError) as e:
        log.warning("couldn't refresh Heroic's Play hook for Cyberpunk: %s", e)
        return []


# --- Nexus downloads (nxm:// links, see modsync.nxm) ----------------------------


def takes_downloads() -> bool:
    """Whether Cyberpunk's nxm:// links are ModSync's to open: the game is in
    Heroic and there's an MO2 instance for it."""
    instance = find_instance()
    return find_game() is not None and instance is not None and instance_problem(instance) is None


def _local_path(game: heroic.GogGame, windows: str) -> Path | None:
    """A Windows path in the game's prefix as the Linux one, through the
    prefix's own drive links: umu maps the home folder to X:, so MO2 started
    from ~/ModOrganizer2-cyberpunk2077 runs as ``X:\\ModOrganizer2-…``."""
    if game.drive_c is None:
        return None
    drive, _, rest = windows.partition(":")
    try:
        root = (game.drive_c.parent / "dosdevices" / f"{drive.lower()}:").resolve(strict=True)
    except OSError:
        return None
    return root.joinpath(*[part for part in rest.split("\\") if part])


def _process_prefix(pid: str) -> Path | None:
    """The Wine prefix a process runs in, from its environment, or None when
    that can't be read."""
    try:
        result = background.run_host(["cat", f"/proc/{pid}/environ"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    for entry in result.stdout.split("\0") if result.returncode == 0 else []:
        if entry.startswith("WINEPREFIX=") and len(entry) > len("WINEPREFIX="):
            return Path(entry[len("WINEPREFIX="):])
    return None


def mo2_running(game: heroic.GogGame, instance: Path) -> bool:
    """Whether this instance's MO2 is running in the game's prefix (open, or
    running the game). Wine names its processes by their Windows path, which
    only means this instance through this prefix's drive letters, so a match
    must also run in this prefix (when its environment can be read)."""
    try:
        found = background.run_host(["pgrep", "-af", r"^[A-Za-z]:\\.*\\ModOrganizer\.exe"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if game.drive_c is None:
        return False
    target = str((instance / "ModOrganizer.exe").resolve()).lower()
    wine_prefix = game.drive_c.parent.resolve()
    for line in found.stdout.splitlines():
        pid, _, command = line.partition(" ")
        match = re.match(r"([A-Za-z]:\\(?:.*?\\)?ModOrganizer\.exe)(?:\s|$)", command, re.IGNORECASE)
        local = _local_path(game, match[1]) if match else None
        if local is None or str(local.resolve()).lower() != target:
            continue
        prefix = _process_prefix(pid)
        if prefix is None or prefix.resolve() == wine_prefix:
            return True
    return False


def _launch_record() -> Path:
    """When ModSync last opened MO2 for a download (its time is the file's)."""
    return state_dir() / "cyberpunk-download-launch"


def _pass_to_mo2(game: heroic.GogGame, instance: Path, url: str) -> str:
    """Give an open MO2 the link. umu's default verb, waitforexitandrun, would
    wait for MO2 to exit first, and "run" keeps Proton's launcher waiting on
    it; runinprefix runs the program alone, in the same Wine session (it exits
    once MO2 has the link)."""
    argv, env = _prefix_command(game, [str(instance / "ModOrganizer.exe"), url], {"PROTON_VERB": "runinprefix"})
    path = state_dir() / "mo2-launch.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as output:
        output.write(b"\n--- ModSync: passing a download to Mod Organizer 2 ---\n")
        output.flush()
        proc = subprocess.Popen(argv, cwd=instance, env=env, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.STDOUT, start_new_session=True)
    try:
        code = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        return "Passed the download to Mod Organizer 2, which is slow to answer."
    if code != 0:
        raise CyberpunkError(f"Mod Organizer 2 didn't take the download (exit code {code}). Details: {path}")
    return "Passed the download to Mod Organizer 2."


# How long MO2 gets to show up once its launch has begun (the hook took the
# link ModSync left), before a later link opens it again.
MO2_START_WAIT = 45.0


def download(url: str, *, poll: float = 2.0) -> str:
    """Hand a Nexus download to Cyberpunk's MO2. An open MO2 gets it from a
    second ModOrganizer.exe, which passes its link to the first and exits. A
    closed one is opened with the link: through Heroic while Heroic's Play hook
    is on, so the session is Heroic's (the hook finds the link ModSync left for
    it), else as 'modsync cyberpunk open' opens it.

    A link that comes while MO2 is still starting for an earlier one waits for
    it rather than opening it again: Heroic ignores a second launch, and a
    second link left for the hook would replace the first. It waits while the
    earlier link is still waiting for the hook (as long as a link left for it
    stays good), then :data:`MO2_START_WAIT` for MO2 to appear. Returns what
    happened."""
    import fcntl
    import time

    game, instance = find_game(), find_instance()
    if game is None:
        raise CyberpunkError("Cyberpunk 2077 wasn't found in Heroic.")
    if instance is None:
        raise CyberpunkError("Cyberpunk 2077 has no Mod Organizer 2 instance ('modsync cyberpunk install').")
    problem = instance_problem(instance)
    if problem:
        raise CyberpunkError(problem)
    if game.drive_c is None:
        raise CyberpunkError("Cyberpunk's prefix isn't set up yet. Launch the game once from Heroic first.")
    note = heroic_hook.download_note(game)
    record = _launch_record()
    record.parent.mkdir(parents=True, exist_ok=True)
    note_gone = None
    while True:
        with record.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one link at a time decides whether to open MO2
            if mo2_running(game, instance):
                lock.truncate(0)  # MO2 is up: no launch is under way any more
                return _pass_to_mo2(game, instance, url)
            now = time.time()
            info = os.fstat(lock.fileno())
            starting = info.st_size > 0 and now < info.st_mtime + heroic_hook.DOWNLOAD_TTL
            if starting:
                note_gone = None if note.exists() else (note_gone or now)
                starting = note_gone is None or now - note_gone < MO2_START_WAIT
            if not starting:
                lock.truncate(0)
                lock.write(f"{now}\n")
                lock.flush()
                return _open_mo2_with(game, instance, url)
        time.sleep(poll)


def _open_mo2_with(game: heroic.GogGame, instance: Path, url: str) -> str:
    refresh_hook()  # the script must be one that knows to look for the link
    hook = heroic_hook.status(game, hook_script(game, instance))
    if hook.enabled and hook.current:
        heroic_hook.leave_download(game, url)
        heroic_hook.launch(game)
        return "Opening Mod Organizer 2 through Heroic to download the file…"
    from modsync.mo2.launch import Launcher

    return Launcher().start(build_plan(instance, game, download=url))


# --- installing MO2 -----------------------------------------------------------


def _ready_game() -> heroic.GogGame:
    game = find_game()
    if game is None:
        raise CyberpunkError("Cyberpunk 2077 wasn't found in Heroic. Install it from GOG in Heroic first.")
    if game.drive_c is None:
        raise CyberpunkError("Cyberpunk's prefix isn't set up yet. Launch the game once from Heroic first.")
    if game_running():
        raise CyberpunkError("Close Cyberpunk 2077 and Mod Organizer 2 first.")
    return game


def game_running() -> bool:
    """Cyberpunk or MO2 running in Wine. A running session holds the prefix.
    Wine names its processes by their Windows path (``Z:\\…\\Cyberpunk2077.exe``),
    which a shell command merely mentioning the program doesn't start with."""
    try:
        found = background.run_host(["pgrep", "-f", r"^[A-Za-z]:\\.*\\(Cyberpunk2077|ModOrganizer)\.exe"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return found.returncode == 0


def _prefix_command(game: heroic.GogGame, args: list[str],
                    env: dict[str, str] | None) -> tuple[list[str], dict[str, str]]:
    argv = [*runner(game), *args]
    full_env = {**prefix_env(game), **(env or {})}
    run_env = dict(os.environ)
    for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH"):
        run_env.pop(key, None)
    run_env.update(full_env)
    if background.in_flatpak():
        argv = ["flatpak-spawn", "--host", "env", *(f"{k}={v}" for k, v in full_env.items()), *argv]
    return argv, run_env


def run_in_prefix(game: heroic.GogGame, args: list[str], *, timeout: float = 900,
                  env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a command to completion with the game's runner in its prefix."""
    argv, run_env = _prefix_command(game, args, env)
    log.info("running %s in Cyberpunk's prefix", " ".join(args))
    return subprocess.run(argv, env=run_env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=timeout)


def winetricks_command(game: heroic.GogGame, verbs: list[str] | tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """winetricks through umu-launcher for a Proton (it runs it in the game's
    runtime), the host's winetricks with the game's Wine otherwise."""
    if game.wine is not None and game.wine.is_proton:
        return ["winetricks", "-q", *verbs], {}
    host = _host_which("winetricks")
    if host is None:
        raise CyberpunkError("winetricks isn't installed, and Cyberpunk runs with a plain Wine in Heroic.")
    return [host, "-q", *verbs], {"WINE": str(game.wine.bin) if game.wine else "wine"}


def _winetricks(game: heroic.GogGame, verbs: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess:
    args, env = winetricks_command(game, verbs)
    if game.wine is not None and game.wine.is_proton:
        return run_in_prefix(game, args)
    return _run_host(args, {**prefix_env(game), **env})


def installed_verbs(game: heroic.GogGame) -> set[str]:
    """The winetricks verbs the prefix already has. umu-launcher refuses a run
    that names any of them, so prepare_prefix leaves those out."""
    result = _winetricks(game, ["list-installed"])
    return {line.strip() for line in result.stdout.splitlines()
            if result.returncode == 0 and line.strip() and " " not in line.strip()}


def prepare_prefix(game: heroic.GogGame, on_output: OnOutput | None = None) -> None:
    """MO2-LINT's prefix steps for Cyberpunk, run the way the game runs."""
    say = on_output or (lambda _line: None)
    installed = installed_verbs(game)
    verbs = [v for v in TRICKS if v not in installed]
    if verbs:
        say("Applying " + ", ".join(verbs) + " to Cyberpunk's prefix…")
        result = _winetricks(game, verbs)
        if result.returncode != 0:
            log.error("winetricks failed (%s): %s\n%s", result.returncode, result.stdout[-2000:], result.stderr[-2000:])
            last = (result.stderr.strip().splitlines() or [""])[-1]
            raise CyberpunkError(f"winetricks exited with code {result.returncode}"
                                 + (f": {last}" if last else ". Details are in ModSync's log."))

    from modsync.mo2 import vcruntime

    system32 = game.drive_c / "windows" / "system32"
    if not vcruntime.prefix_runtime_problems(system32):
        return
    say("Installing Microsoft's Visual C++ runtime into Cyberpunk's prefix…")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = vcruntime.backup_prefix_runtime(system32, config.data_dir() / "backups" / f"cyberpunk-vcruntime-{stamp}")
    result = run_in_prefix(game, [str(vcruntime.redist_installer()), "/install", "/quiet", "/norestart"])
    if not vcruntime.installer_succeeded(result.returncode):
        log.error("VC++ runtime install failed (%s): %s\n%s", result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        raise CyberpunkError(f"The Visual C++ runtime installer exited with code {result.returncode}. "
                             f"The previous DLLs are backed up in {backup}.")
    left = vcruntime.prefix_runtime_problems(system32)
    if left:
        raise CyberpunkError("The Visual C++ runtime installer finished, but the prefix still has "
                             + "; ".join(left) + f". The previous DLLs are backed up in {backup}.")


def _run_host(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    if background.in_flatpak():
        argv = ["flatpak-spawn", "--host", "env", *(f"{k}={v}" for k, v in env.items()), *argv]
    return subprocess.run(argv, env={**os.environ, **env}, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=900)


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


def default_instance_dir() -> Path:
    return Path.home() / f"ModOrganizer2-{CYBERPUNK.mo2lint_key}"


def install_mo2(dest: Path | str, on_output: OnOutput | None = None, *, backend=None) -> Path:
    """A Cyberpunk MO2 instance in ``dest``: MO2-LINT's install (MO2, its config
    and Heroic's "Launch Mod Organizer" entry), the prefix steps, RootBuilder.
    Then it's the instance in use. A Cyberpunk instance already in ``dest``
    (an install a later step stopped, or one to repair) skips MO2-LINT and gets
    the rest, each step doing only what's missing."""
    from modsync.mo2.installers import Mo2LintBackend

    say = on_output or (lambda _line: None)
    game = _ready_game()
    instance = Path(dest).expanduser()
    if _is_instance(instance):
        problem = instance_problem(instance)
        if problem:
            raise CyberpunkError(problem)
        say(f"MO2 is already in {instance}; finishing its setup.")
    else:
        backend = backend or Mo2LintBackend()
        ok, reason = backend.available()
        if not ok:
            raise CyberpunkError(f"Cannot install: {reason}")
        result = backend.install(CYBERPUNK, instance, launcher="gog", env=MO2LINT_ENV, on_output=on_output)
        if not result.success or result.instance_path is None:
            raise CyberpunkError(f"Install failed: {result.message}")
        instance = result.instance_path
    try:
        prepare_prefix(game, on_output)
        say("Adding the RootBuilder plugin (it puts CET's and RED4ext's DLLs next to the game while you play)…")
        install_rootbuilder(instance)
    except (CyberpunkError, OSError, zipfile.BadZipFile) as e:
        raise CyberpunkError(f"{e} MO2 is installed in {instance}; run the install again to finish its setup.") from e
    chosen = use_instance(instance)
    for note in refresh_hook():
        say(note)
    return chosen


# --- syncing the mods -----------------------------------------------------------

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


def _synced_instance() -> Path | None:
    """The instance handed to ModSync, never one it only found: syncing that
    would change a folder nobody gave it."""
    chosen = chosen_instance()
    return chosen if chosen is not None and instance_problem(chosen) is None else None


def game_version(game: heroic.GogGame | None = None) -> tuple[str, str] | None:
    """The game here as (patch, build): ``("2.5", "3.0.88.46235")``."""
    game = game or find_game()
    if game is None:
        return None
    build, product = pe.build_and_product(game.install_path / CYBERPUNK.exe_name)
    if build is None:
        return None
    return product or ".".join(map(str, build)), ".".join(map(str, build))


_RECORD_STEM, _RECORD_EXT = VAULT_RECORD.rsplit(".", 1)
RECORD_CONFLICTS = f"{_RECORD_STEM}.sync-conflict-*.{_RECORD_EXT}"


def _record_vault_version(root: Path) -> None:
    """The machine that starts syncing the mods says which game version they're
    for. When it can't read the game's version yet, it remembers that it owes
    the record (:func:`_owed_record`); a machine that joins never writes one."""
    record = root / VAULT_RECORD
    if record.exists():
        return
    version = game_version()
    if version is None:
        _update_settings(record_owed=True)
        return
    record.write_text(json.dumps({"game_version": version[0], "build": version[1]}, indent=2) + "\n",
                      encoding="utf-8")
    _update_settings(record_owed=None)


def _owed_record(root: Path) -> None:
    """Run whenever the folder's status is read: write a record owed since sharing."""
    if _settings().get("record_owed"):
        _record_vault_version(root)


def _read_record(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("build"), str):
        return None
    return str(data.get("game_version") or data["build"]), data["build"]


def vault_versions(instance: Path | None = None) -> list[tuple[str, str]]:
    """Every version the synced records name: two machines that each started
    sharing before they met leave a conflict copy of the record too."""
    instance = instance or _synced_instance()
    if instance is None:
        return []
    paths = [instance / VAULT_RECORD, *sorted(instance.glob(RECORD_CONFLICTS))]
    return list(dict.fromkeys(v for p in paths if (v := _read_record(p))))


def vault_version(instance: Path | None = None) -> tuple[str, str] | None:
    return _read_record(instance / VAULT_RECORD) if (instance := instance or _synced_instance()) else None


def version_problem(game: heroic.GogGame | None = None, instance: Path | None = None) -> str | None:
    """A note when the game here isn't the version the synced mods are for.
    Only a note: frameworks like CET and RED4ext need a version they support,
    but whether to change versions (Heroic can install older GOG builds) is
    the player's call."""
    here = game_version(game)
    others = [v for v in vault_versions(instance) if here is not None and v[1] != here[1]]
    if not others:
        return None
    wanted = " or ".join(f"{v[0]} (build {v[1]})" for v in others)
    return (f"Cyberpunk 2077 here is {here[0]} (build {here[1]}), but the synced mods were set up for "
            f"{wanted}. Mods made for another version may not work, CET and RED4ext "
            "especially. Heroic can install another GOG build under the game's settings.")


def _accepts_offer(root: Path) -> bool:
    """A paired machine's mods fill an instance by themselves only while it
    has none installed: merging two setups (both machines' mods enabled in
    each other's profiles) is for 'modsync sync share cp2077-mo2' to choose."""
    try:
        return not any((root / "mods").iterdir())
    except OSError:
        return True  # no mods folder: nothing to merge with


MODS_FOLDER = FolderSpec(
    key="cp2077-mo2",
    label="Cyberpunk 2077 mods (ModSync)",
    path=_synced_instance,
    ignore=(
        "// Managed by ModSync. Syncs Cyberpunk 2077's Mod Organizer 2 content: mods, profiles,",
        "// downloads and overwrite. Never the MO2 program, ModOrganizer.ini, nexusApiKey,",
        "// or what playing leaves behind.",
        "",
        *PLAY_LEFTOVERS,
        "",
        *(f"!/{name}" for name in stignore.CONTENT_DIRS),
        "!/categories.dat",
        f"!/{VAULT_RECORD}",
        f"!/{RECORD_CONFLICTS}",
        "",
        "// Everything else at the instance root. Ignoring a folder skips what's in it.",
        "/*",
    ),
    content=(*stignore.CONTENT_DIRS, "categories.dat", VAULT_RECORD),
    on_share=_record_vault_version,
    in_use=lambda: game_running(),
    tidy=_owed_record,
    accepts=_accepts_offer,
)


# --- for the app ------------------------------------------------------------------


@dataclass
class Overview:
    """Everything the app shows about Cyberpunk, read in one go (off the UI
    thread: it reads Heroic's files and the game's executable)."""

    game: heroic.GogGame | None
    instance: Path | None  # the MO2 instance in use: the one chosen, else one MO2-LINT installed
    chosen: bool  # handed to ModSync (install or use): only that one syncs
    instance_problem: str | None
    hook: heroic_hook.HookStatus | None
    mods: str  # "on" | "off" | "stopping"
    version: tuple[str, str] | None  # the game here: (patch, build)
    version_problem: str | None
    prefix_ready: bool = False  # Heroic has made the game's prefix (read here, off the UI thread)
    needs_setup: bool = False  # the instance lacks RootBuilder: an install stopped part way, or MO2-LINT's own


def setup_finished(instance: Path) -> bool:
    """Whether ``install_mo2`` got to its last step for this instance."""
    return (instance / "plugins" / "rootbuilder" / "__init__.py").is_file()


def overview(*, hook: bool = True) -> Overview:
    """``hook``: also read the Play hook (Heroic's settings for the game)."""
    from modsync.state import State
    from modsync.sync import folders

    game, instance = find_game(), find_instance()
    st = State.load()
    key = MODS_FOLDER.key
    mods = "stopping" if folders.stopping(key, st) else "on" if key in st.game_folders else "off"
    status = None
    if hook and game is not None:
        try:
            status = hook_status()
        except Exception as exc:  # Heroic's files unreadable: show the rest
            log.warning("could not read Heroic's Play hook for Cyberpunk: %s", exc)
    chosen = instance is not None and instance == chosen_instance()
    problem = instance_problem(instance) if instance else None
    return Overview(game, instance, chosen, problem, status, mods,
                    game_version(game) if game else None, version_problem(game) if game else None,
                    prefix_ready=game is not None and game.drive_c is not None,
                    needs_setup=instance is not None and problem is None and not setup_finished(instance))


def launch(launcher, *, play: bool) -> str:
    """Open MO2, or play through it, from the app."""
    refresh_hook()
    plan = build_plan(find_instance(), find_game(), play=play)
    return launcher.start(plan)
