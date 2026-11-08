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
no mod's copy, so it is harmless without mods.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

from modsync import background
from modsync.games import CYBERPUNK
from modsync.heroic import library as heroic
from modsync.mo2 import ini
from modsync.mo2.launch import LaunchPlan, nested_desktop_display
from modsync.state import State

log = logging.getLogger(__name__)

DLL_OVERRIDES = "winmm,version=n,b"
_OVERRIDDEN = {"winmm", "version"}


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


def find_instance() -> Path | None:
    """The instance ``modsync cyberpunk use`` chose, else one MO2-LINT installed."""
    chosen = State.load().cyberpunk_instance
    if chosen and _is_instance(Path(chosen)):
        return Path(chosen)
    return next((p for p in _mo2lint_instances() if _is_instance(p)), None)


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
    State.update(cyberpunk_instance=str(instance))
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
    return ["run", "-a", "--launcher-skip -modded", "-c", "Z:" + str(exe.parent), "Z:" + str(exe)], CYBERPUNK.name


def build_plan(instance_path: Path | str | None, game: heroic.GogGame | None, *, play: bool = False) -> LaunchPlan:
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
    command = [str(instance / "ModOrganizer.exe"), *mo2_args]

    env = {
        "WINEPREFIX": str(game.prefix),
        "WINEDLLOVERRIDES": with_dll_overrides(os.environ.get("WINEDLLOVERRIDES")),
    }
    proton = game.wine.proton_dir
    if proton is not None:
        env.update({
            "STEAM_COMPAT_DATA_PATH": str(game.prefix),
            "STEAM_COMPAT_INSTALL_PATH": str(game.install_path),
            "PROTONPATH": str(proton),
            "GAMEID": f"umu-{CYBERPUNK.appid}",
            "STORE": "gog",
        })
    display = nested_desktop_display()
    if display:
        env.update({"DISPLAY": display, "ENABLE_GAMESCOPE_WSI": "0", "DISABLE_GAMESCOPE_WSI": "1"})
    return LaunchPlan([*command_runner, *command], env, instance, target, play, proton, command)
