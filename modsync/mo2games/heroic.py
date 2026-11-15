"""A game from GOG or Epic, installed and run by the Heroic Games Launcher
(the Heroic half of :mod:`modsync.mo2games`).

Heroic runs the game with umu-launcher and the Proton chosen in its settings,
in a prefix of its own (:mod:`modsync.heroic.library`). MO2 runs the same way:
``umu-run ModOrganizer.exe`` with Heroic's prefix and Proton, so the game MO2
starts finds the prefix exactly as Heroic left it. ``GAMEID`` is the game's
Steam id with umu's prefix, as Heroic sets it, so umu's protonfixes for the
game apply.

MO2-LINT installs MO2 itself, but for Heroic's stores its prefix steps run
winetricks with whatever ``wine`` is on PATH (a host Wine on a desktop, which
upgrades the Proton prefix and stops at a Wine Mono dialog; none on a Steam
Deck) and pass the VC++ installer and DLL overrides to winetricks as verbs,
which fail. :data:`MO2LINT_ENV` makes each of those steps fail at once without
touching the prefix, and :func:`prepare_prefix` does them with the game's own
runner.

Each function takes the :class:`~modsync.mo2games.ManagedGame` and the game as
Heroic has it (a ``HeroicGame``).
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath

from modsync import background, config
from modsync.heroic import hook as heroic_hook
from modsync.logging_setup import state_dir
from modsync.mo2 import downloads, ini
from modsync.mo2.launch import LaunchPlan, nested_desktop_display, with_dll_overrides

log = logging.getLogger(__name__)

LAUNCHER = "Heroic"

MO2LINT_ENV = {"WINE": "/bin/false", "WINELOADER": "/bin/false", "WINESERVER": "/bin/true"}
# What umu-launcher calls each store (its STORE variable).
UMU_STORES = {"gog": "gog", "epic": "egs"}


def not_ready(mg, game) -> str | None:
    """Why nothing can run in the game's prefix yet, or None."""
    if game.drive_c is None:
        return f"{mg.short}'s prefix isn't set up yet. Launch the game once from Heroic first."
    return None


def install_blocked(mg, game) -> str | None:
    return None


# --- launching ----------------------------------------------------------------


def runner(mg, game) -> list[str]:
    """What runs a Windows program in the game's prefix: umu-launcher for a
    Proton (the system's, else the copy Heroic downloads), the Wine binary
    itself for a plain Wine."""
    from modsync import mo2games

    if game.wine is None:
        raise mg.Error(f"Heroic has no Wine or Proton set for {mg.short}. Pick one in its settings.")
    if not game.wine.is_proton:
        return [str(game.wine.bin)]
    system = mo2games.host_which("umu-run")
    if system:
        return [system]
    bundled = game.config_dir / "tools" / "runtimes" / "umu" / "umu_run.py"
    if bundled.is_file():
        return ["python3", str(bundled)]
    raise mg.Error(f"umu-launcher wasn't found. Launch {mg.short} once from Heroic, which downloads it.")


def prefix_env(mg, game) -> dict[str, str]:
    """What :func:`runner` needs to run in the game's prefix, as Heroic sets it."""
    env = {"WINEPREFIX": str(game.prefix)}
    if mg.game.dll_overrides:
        env["WINEDLLOVERRIDES"] = with_dll_overrides(os.environ.get("WINEDLLOVERRIDES"), mg.game.dll_overrides)
    proton = game.wine.proton_dir if game.wine is not None else None
    if proton is not None:
        env.update({
            "STEAM_COMPAT_DATA_PATH": str(game.prefix),
            "STEAM_COMPAT_INSTALL_PATH": str(game.install_path),
            "PROTONPATH": str(proton),
            "GAMEID": f"umu-{mg.game.appid}" if mg.game.appid else "umu-default",
            "STORE": UMU_STORES.get(game.store, game.store),
        })
    return env


def build_plan(mg, instance: Path, game, *, play: bool = False, download: str | None = None) -> LaunchPlan:
    command_runner = runner(mg, game)
    sections = ini.read_ini(instance / "ModOrganizer.ini")
    general = sections.get("General", {})
    mo2_args: list[str] = []
    profile = ini.unwrap_bytearray(ini.get_ci(general, "selected_profile"))
    if profile:
        mo2_args += ["-p", profile]
    target = "Mod Organizer 2"
    if play:
        args, target = mg.play_target(sections, game.install_path, game.store)
        mo2_args += args
    elif download:
        mo2_args.append(download)
    command = [str(instance / "ModOrganizer.exe"), *mo2_args]
    env = prefix_env(mg, game)
    display = nested_desktop_display()
    if display:
        env.update({"DISPLAY": display, "ENABLE_GAMESCOPE_WSI": "0", "DISABLE_GAMESCOPE_WSI": "1"})
    return LaunchPlan([*command_runner, *command], env, instance, target, play, game.wine.proton_dir, command)


def prepare(mg, game, plan: LaunchPlan) -> None:
    """Nothing to do before MO2 starts: :func:`prepare_prefix` readied the prefix."""


def prefix_command(mg, game, args: list[str], env: dict[str, str] | None) -> tuple[list[str], dict[str, str]]:
    argv = [*runner(mg, game), *args]
    full_env = {**prefix_env(mg, game), **(env or {})}
    run_env = dict(os.environ)
    for key in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH"):
        run_env.pop(key, None)
    run_env.update(full_env)
    if background.in_flatpak():
        argv = ["flatpak-spawn", "--host", "env", *(f"{k}={v}" for k, v in full_env.items()), *argv]
    return argv, run_env


def run_in_prefix(mg, game, args: list[str], *, timeout: float = 900,
                  env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a command to completion with the game's runner in its prefix."""
    argv, run_env = prefix_command(mg, game, args, env)
    log.info("running %s in %s's prefix", " ".join(args), mg.short)
    return subprocess.run(argv, env=run_env, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=timeout)


# --- Heroic's Play button -----------------------------------------------------


def hook_script(mg, game, instance: Path) -> str:
    """The Play hook as it should be now. Like Play, it runs the program MO2
    lists for playing; it passes no profile, so MO2 plays whichever profile
    was last chosen in it."""
    return heroic_hook.render(game, game_name=mg.name, instance=instance.resolve(),
                              game_exe=PureWindowsPath(mg.game.play_exes[0]).name if mg.game.play_exes else "",
                              prefer_argument=mg.game.play_argument,
                              direct_args=mg.direct_args(game.install_path, game.store),
                              disable_command=f"{mg.command} hook disable",
                              dll_overrides=mg.game.dll_overrides)


def hook_status(mg, game, instance: Path | None) -> heroic_hook.HookStatus:
    return heroic_hook.status(game, hook_script(mg, game, instance) if instance is not None else None)


def enable_hook(mg, game, instance: Path) -> list[str]:
    try:
        return heroic_hook.enable(game, hook_script(mg, game, instance), instance)
    except heroic_hook.HookError as e:
        raise mg.Error(str(e)) from e


def disable_hook(mg, game) -> str:
    try:
        return heroic_hook.disable(game)
    except heroic_hook.HookError as e:
        raise mg.Error(str(e)) from e


def refresh_hook(mg, game, instance: Path) -> list[str]:
    return heroic_hook.refresh(game, hook_script(mg, game, instance), instance)


def through_hook(mg, game, instance: Path) -> bool:
    """Whether Heroic's Play hook is on and knows what it should, so a launch
    of the game in Heroic runs MO2 (call refresh_hook first)."""
    hook = heroic_hook.status(game, hook_script(mg, game, instance))
    return hook.enabled and hook.current


def ask_store(mg, game, note: str) -> None:
    """Have Heroic launch the game, with ``note`` (an nxm:// link, or the
    hook's PLAY or OPEN) left for the hook to take."""
    if note in (heroic_hook.PLAY, heroic_hook.OPEN):
        heroic_hook.leave_request(game, note)
    else:
        heroic_hook.leave_download(game, note)
    try:
        heroic_hook.launch(game)
    except Exception:
        heroic_hook.launch_note(game).unlink(missing_ok=True)  # no launch will take it
        raise


# --- Nexus downloads ------------------------------------------------------------


def mo2_running(mg, game, instance: Path) -> bool:
    """Whether this instance's MO2 is running in the game's prefix (open, or
    running the game)."""
    return game.drive_c is not None and downloads.mo2_running(game.drive_c.parent, instance)


def _pass_to_mo2(mg, game, instance: Path, url: str) -> str:
    """Give an open MO2 the link. umu's default verb, waitforexitandrun, would
    wait for MO2 to exit first, and "run" keeps Proton's launcher waiting on
    it; runinprefix runs the program alone, in the same Wine session (it exits
    once MO2 has the link)."""
    argv, env = prefix_command(mg, game, [str(instance / "ModOrganizer.exe"), url], {"PROTON_VERB": "runinprefix"})
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
        raise mg.Error(f"Mod Organizer 2 didn't take the download (exit code {code}). Details: {path}")
    return "Passed the download to Mod Organizer 2."


@dataclass
class HeroicDownloads:
    """A Heroic game's MO2 as a download reaches it (see modsync.mo2.downloads)."""

    mg: object
    game: object
    instance: Path

    @property
    def record(self) -> Path:
        return self.mg.launch_record()

    @property
    def note(self) -> Path:
        return heroic_hook.launch_note(self.game)

    @property
    def taken(self) -> Path:
        return heroic_hook.launch_taken(self.game)

    def running(self) -> bool:
        return mo2_running(self.mg, self.game, self.instance)

    def pass_link(self, url: str) -> str:
        return _pass_to_mo2(self.mg, self.game, self.instance, url)

    def launching(self) -> str | None:
        return None

    def through_hook(self) -> bool:
        self.mg.refresh_hook()  # the script must be one that knows to look for the link
        return through_hook(self.mg, self.game, self.instance)

    def ask_hook(self, url: str) -> str:
        ask_store(self.mg, self.game, url)
        return "Opening Mod Organizer 2 through Heroic to download the file…"

    def open_direct(self, url: str) -> str:
        from modsync.mo2.launch import Launcher

        return Launcher().start(self.mg.build_plan(self.instance, self.game, download=url))


def downloads_target(mg, game, instance: Path) -> HeroicDownloads:
    return HeroicDownloads(mg, game, instance)


def download(mg, game, instance: Path, url: str, *, poll: float = 2.0, on_wait=None) -> str:
    return downloads.download(downloads_target(mg, game, instance), url, poll=poll, on_wait=on_wait)


# --- installing MO2 -----------------------------------------------------------


def mo2lint_install(mg, game, backend, dest: Path, on_output):
    """MO2-LINT's install for the game's store in Heroic: MO2, its config and
    Heroic's "Launch Mod Organizer" entry. Its prefix steps are held off
    (:data:`MO2LINT_ENV`); :func:`prepare_prefix` does them."""
    return backend.install(mg.game, dest, launcher=game.store, env=MO2LINT_ENV, on_output=on_output)


def winetricks_command(mg, game, verbs: list[str] | tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """winetricks through umu-launcher for a Proton (it runs it in the game's
    runtime), the host's winetricks with the game's Wine otherwise."""
    from modsync import mo2games

    if game.wine is not None and game.wine.is_proton:
        return ["winetricks", "-q", *verbs], {}
    host = mo2games.host_which("winetricks")
    if host is None:
        raise mg.Error(f"winetricks isn't installed, and {mg.short} runs with a plain Wine in Heroic.")
    return [host, "-q", *verbs], {"WINE": str(game.wine.bin) if game.wine else "wine"}


def _winetricks(mg, game, verbs: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess:
    args, env = winetricks_command(mg, game, verbs)
    if game.wine is not None and game.wine.is_proton:
        return run_in_prefix(mg, game, args)
    return _run_host(args, {**prefix_env(mg, game), **env})


def installed_verbs(mg, game) -> set[str]:
    """The winetricks verbs the prefix already has. umu-launcher refuses a run
    that names any of them, so prepare_prefix leaves those out."""
    result = _winetricks(mg, game, ["list-installed"])
    return {line.strip() for line in result.stdout.splitlines()
            if result.returncode == 0 and line.strip() and " " not in line.strip()}


def tricks(mg) -> tuple[str, ...]:
    """MO2-LINT's own prefix steps for the game, less vcrun2022: winetricks'
    vcrun2022 can leave a broken runtime (MO2-LINT #1106), so prepare_prefix
    runs Microsoft's redistributable instead, as ModSync does for Skyrim."""
    from modsync import mo2games

    return (*mo2games.BASE_TRICKS, *mg.game.tricks)


def prepare_prefix(mg, game, on_output=None) -> None:
    """MO2-LINT's prefix steps for the game, run the way the game runs."""
    say = on_output or (lambda _line: None)
    installed = installed_verbs(mg, game)
    verbs = [v for v in tricks(mg) if v not in installed]
    if verbs:
        say("Applying " + ", ".join(verbs) + f" to {mg.short}'s prefix…")
        result = _winetricks(mg, game, verbs)
        if result.returncode != 0:
            log.error("winetricks failed (%s): %s\n%s", result.returncode, result.stdout[-2000:], result.stderr[-2000:])
            last = (result.stderr.strip().splitlines() or [""])[-1]
            raise mg.Error(f"winetricks exited with code {result.returncode}"
                           + (f": {last}" if last else ". Details are in ModSync's log."))

    from modsync.mo2 import vcruntime

    system32 = game.drive_c / "windows" / "system32"
    if not vcruntime.prefix_runtime_problems(system32):
        return
    say(f"Installing Microsoft's Visual C++ runtime into {mg.short}'s prefix…")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = vcruntime.backup_prefix_runtime(system32, config.data_dir() / "backups" / f"{mg.slug}-vcruntime-{stamp}")
    result = run_in_prefix(mg, game, [str(vcruntime.redist_installer()), "/install", "/quiet", "/norestart"])
    if not vcruntime.installer_succeeded(result.returncode):
        log.error("VC++ runtime install failed (%s): %s\n%s", result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        raise mg.Error(f"The Visual C++ runtime installer exited with code {result.returncode}. "
                       f"The previous DLLs are backed up in {backup}.")
    left = vcruntime.prefix_runtime_problems(system32)
    if left:
        raise mg.Error("The Visual C++ runtime installer finished, but the prefix still has "
                       + "; ".join(left) + f". The previous DLLs are backed up in {backup}.")


def _run_host(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    if background.in_flatpak():
        argv = ["flatpak-spawn", "--host", "env", *(f"{k}={v}" for k, v in env.items()), *argv]
    return subprocess.run(argv, env={**os.environ, **env}, stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, errors="replace", timeout=900)
