"""Launch the chosen portable MO2 instance using Skyrim's existing Proton prefix.

The Steam launch hook (``modsync.launchhook``) covers the other direction:
Steam's Play button starting ModSync. This module is for ModSync's own buttons,
where Steam is not the caller, and shares the hook's view of which Proton and
runtime the game uses.

Direct play uses MO2's `run` command, so its virtual filesystem and selected
profile remain active. Steam launch mappings and MO2 settings are never edited.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import threading

from modsync import background, launchhook, platforms
from modsync.games import SKYRIM_SE
from modsync.logging_setup import state_dir
from modsync.mo2 import ini
from modsync.steam import compattools, libraries, shortcuts
from modsync.steam.appmanifest import AppManifest

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaunchPlan:
    argv: list[str]
    env: dict[str, str]
    cwd: Path
    target: str
    play: bool


def _game_target(sections: dict, game_dir: Path) -> tuple[list[str], str]:
    """Prefer configured SKSE/Skyrim entries, preserving their MO2 arguments."""
    entries: dict[str, dict[str, str]] = {}
    for key, value in sections.get("customExecutables", {}).items():
        match = re.fullmatch(r"(\d+)\\(title|binary)", key, re.IGNORECASE)
        if match:
            entries.setdefault(match[1], {})[match[2].lower()] = ini.unwrap_bytearray(value) or ""
    for filename, fallback_name in (("skse64_loader.exe", "SKSE"), ("SkyrimSE.exe", "Skyrim")):
        for entry in entries.values():
            if not entry.get("title") or PureWindowsPath(entry.get("binary", "")).name.lower() != filename.lower():
                continue
            local = ini.wine_to_local(entry.get("binary"))
            if local is not None and not local.is_file():
                # A stale entry (e.g. SKSE removed by a reinstall) would only make
                # MO2 fail; fall through to whatever is actually on disk.
                continue
            return ["run", "-e", entry["title"]], entry["title"]
        binary = game_dir / filename
        if binary.is_file():
            # A new instance may not have saved its executable list yet. Still
            # run the file THROUGH MO2, with the correct working directory.
            return ["run", "-c", "Z:" + str(game_dir), "Z:" + str(binary)], fallback_name
    raise RuntimeError("Skyrim's executable was not found. Open MO2 and check its game folder.")


def _proton_for(compat: Path, libs: list) -> Path:
    """The Proton to run MO2 with: Steam's current choice for the game (seen
    through ModSync's launch hook), else the one that last set up the prefix."""
    tool = launchhook.game_proton(SKYRIM_SE.appid)
    if tool is not None:
        return tool.path
    try:
        config_info = (compat / "config_info").read_text(encoding="utf-8", errors="replace")
    except OSError:
        config_info = ""
    for line in config_info.splitlines():
        for marker in ("/files/", "/dist/"):
            if marker in line:
                candidate = Path(line.split(marker, 1)[0])
                if candidate.is_absolute() and (candidate / "proton").is_file():
                    return candidate
    raise RuntimeError(
        "Skyrim's Proton wasn't found. Pick one under Properties → Compatibility in Steam, "
        "or launch Skyrim once through Steam, then try again."
    )


def build_plan(instance_path: Path | str | None, *, play: bool = False) -> LaunchPlan:
    if not instance_path:
        raise RuntimeError("Choose an MO2 instance first.")
    instance = Path(instance_path).expanduser().resolve()
    exe = instance / "ModOrganizer.exe"
    if not exe.is_file():
        raise RuntimeError("ModOrganizer.exe is missing from the chosen folder. Choose or install MO2 again.")
    sections = ini.read_ini(instance / "ModOrganizer.ini")
    general = sections.get("General", {})
    name = ini.unwrap_bytearray(ini.get_ci(general, "gameName"))
    if name and name != SKYRIM_SE.mo2_game_name:
        raise RuntimeError("The chosen MO2 instance is not configured for Skyrim Special Edition.")

    for root in platforms.current().steam_roots():
        libs = libraries.read_libraries(root)
        app = libraries.find_app(libs, SKYRIM_SE.appid)
        if app is not None:
            break
    else:
        raise RuntimeError("Skyrim wasn't found in Steam. Install it and launch it once through Steam first.")
    manifest = AppManifest.load(app.library.steamapps / f"appmanifest_{SKYRIM_SE.appid}.acf")
    if manifest.update_in_progress:
        raise RuntimeError("Steam is updating Skyrim. Wait for it to finish before launching MO2 or playing.")
    compat = app.library.steamapps / "compatdata" / str(SKYRIM_SE.appid)
    if not (compat / "pfx").is_dir():
        raise RuntimeError("Skyrim's Proton setup is missing. Launch Skyrim once through Steam first.")
    proton = _proton_for(compat, libs)

    # Passing no profile preserves MO2's own choice. The portable instance is
    # pinned with MO2's portable.txt marker at launch time (see Launcher.start):
    # `-i ""` makes MO2 2.5 exit silently before it writes any log.
    mo2_args: list[str] = []
    profile = ini.unwrap_bytearray(ini.get_ci(general, "selected_profile"))
    if profile:
        mo2_args += ["-p", profile]
    target = "Mod Organizer 2"
    if play:
        if not shortcuts.steam_is_running():
            raise RuntimeError("Start Steam, then try Play Skyrim again.")
        if not sections:
            raise RuntimeError("Open MO2 and finish setting up the instance before playing.")
        raw_game_dir = ini.unwrap_bytearray(ini.get_ci(general, "gamePath"))
        configured_dir = ini.wine_to_local(raw_game_dir)
        if raw_game_dir and configured_dir is None:
            windows = PureWindowsPath(raw_game_dir)
            if windows.drive and windows.root:
                configured_dir = compat / "pfx" / "dosdevices" / windows.drive.lower()
                configured_dir = configured_dir.joinpath(*windows.parts[1:]).resolve()
            else:
                raise RuntimeError("MO2's game folder could not be resolved. Check its game path in MO2.")
        game_dir = configured_dir or app.install_path
        args, target = _game_target(sections, game_dir)
        mo2_args += args
    argv = [str(proton / "proton"), "run", str(exe), *mo2_args]
    runtime_id = compattools.require_tool_appid(proton)
    tool_paths = [str(proton)]
    if runtime_id is not None:
        runtime = compattools.runtime_path(runtime_id, libs)
        if runtime is None:
            raise RuntimeError(f"Steam Linux Runtime {runtime_id} is missing. Launch Skyrim once through Steam to install it.")
        argv = [str(runtime / "_v2-entry-point"), "--verb=run", "--", *argv]
        tool_paths.append(str(runtime))
    env = {
        "STEAM_COMPAT_DATA_PATH": str(compat),
        "STEAM_COMPAT_CLIENT_INSTALL_PATH": str(root),
        "STEAM_COMPAT_LIBRARY_PATHS": ":".join(str(lib.path) for lib in libs),
        "STEAM_COMPAT_TOOL_PATHS": ":".join(tool_paths),
        "SteamAppId": str(SKYRIM_SE.appid),
        "SteamGameId": str(SKYRIM_SE.appid),
        "STEAM_COMPAT_APP_ID": str(SKYRIM_SE.appid),
    }
    return LaunchPlan(argv, env, instance, target, play)


class Launcher:
    """Track our launch processes without tying their lifetime to the GUI."""

    def __init__(self) -> None:
        self._processes: list[tuple[subprocess.Popen, LaunchPlan, Path]] = []
        self._lock = threading.Lock()

    def running(self, *, play: bool | None = None) -> bool:
        with self._lock:
            return any(p.poll() is None and (play is None or plan.play == play)
                       for p, plan, _ in self._processes)

    def start(self, plan: LaunchPlan) -> str:
        with self._lock:
            if any(p.poll() is None and item.play == plan.play for p, item, _ in self._processes):
                raise RuntimeError("That launch is already running.")
            path = state_dir() / "mo2-launch.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 1_000_000 and not any(p.poll() is None for p, _, _ in self._processes):
                path.replace(path.with_suffix(".log.1"))
            # MO2's own marker for a portable install: it makes MO2 open the
            # instance next to ModOrganizer.exe even when the prefix remembers a
            # different global instance. Nothing else is written.
            (plan.cwd / "portable.txt").touch(exist_ok=True)
            env = dict(os.environ)
            # Steam/Qt preload settings belong to the calling GUI, not Proton.
            for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH"):
                env.pop(key, None)
            env.update(plan.env)
            argv = plan.argv
            if background.in_flatpak():
                argv = ["flatpak-spawn", "--host", f"--directory={plan.cwd}",
                        "env", *(f"{k}={v}" for k, v in plan.env.items()), *argv]
            with path.open("ab") as output:
                output.write(f"\n--- ModSync: launching {plan.target} ---\n".encode())
                output.flush()
                proc = subprocess.Popen(argv, cwd=plan.cwd, env=env, stdin=subprocess.DEVNULL,
                                        stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            self._processes.append((proc, plan, path))
            log.info("launching %s through MO2 in %s (pid %s)", plan.target, plan.cwd, proc.pid)
        return f"Starting {plan.target}" + (" through MO2…" if plan.play else "…")

    def poll(self) -> list[str]:
        errors = []
        with self._lock:
            active = []
            for proc, plan, path in self._processes:
                code = proc.poll()
                if code is None:
                    active.append((proc, plan, path))
                elif code:
                    message = f"{plan.target} exited with code {code}. Launch details: {path}"
                    log.error(message)
                    errors.append(message)
            self._processes = active
        return errors
