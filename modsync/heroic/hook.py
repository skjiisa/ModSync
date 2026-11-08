"""Heroic's Play button on a GOG game runs Mod Organizer 2: the Heroic
counterpart of Steam's launch hook (:mod:`modsync.launchhook`).

Heroic has no compatibility tools to stand in for, but each game's settings
take *wrappers*: programs Heroic puts ahead of the game's command line. The
hook is one, :file:`play_hook.sh`, first in the game's ``wrapperOptions`` in
``GamesConfig/<appName>.json``. It keeps the command that runs the game (umu-run
with Heroic's Proton and prefix) and runs MO2 in place of the game's program,
so the game stays Heroic's launch: Heroic tracks it and syncs GOG cloud saves.

The script lives in Heroic's config folder, because Heroic's Flatpak can't read
the rest of the home folder or start programs outside its sandbox. So the hook
can't ask ModSync anything at launch: the instance and MO2's arguments are
written into it, and :func:`refresh` rewrites it when they change (which needs
no Heroic restart: Heroic reads only the wrapper's path from its settings).
For the same reason Heroic's Flatpak gets access to an instance outside the
folders it can already see, through a ``flatpak override`` for that folder.

Heroic keeps a game's settings in memory while it runs and writes them back
when one changes, so the settings file is only edited while Heroic is closed.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from modsync import __version__, background
from modsync.heroic.library import GogGame

HOOK_VERSION = 1
TEMPLATE = Path(__file__).parent / "play_hook.sh"
MARKER = "--modsync-heroic-hook"
FLATPAK_ID = "com.heroicgameslauncher.hgl"
# What Heroic's Flatpak can read (its finish-args), relative to the home folder
# unless absolute.
FLATPAK_VISIBLE = ("Games/Heroic", "Documents", "Desktop", ".steam", "/mnt", "/media", "/run/media")


class HookError(RuntimeError):
    pass


@dataclass(frozen=True)
class HookStatus:
    enabled: bool  # in the game's wrappers in Heroic
    current: bool  # the script says what it should (always False when it's missing)
    script: Path

    def summary(self) -> str:
        if not self.enabled:
            return "off"
        return "on" if self.current else "on, but out of date (ModSync rewrites it on its next launch)"


def script_path(game: GogGame) -> Path:
    return game.config_dir / "modsync" / f"play-{game.app_name}.sh"


def _settings_path(game: GogGame) -> Path:
    return game.config_dir / "GamesConfig" / f"{game.app_name}.json"


def render(game: GogGame, *, game_name: str, instance: Path, play_args: list[str],
           open_args: list[str], disable_command: str) -> str:
    def words(args: list[str]) -> str:
        return " ".join(shlex.quote(a) for a in args)

    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in {
        "GAME_NAME": game_name,
        "APP_NAME": game.app_name,
        "MODSYNC_VERSION": __version__,
        "HOOK_VERSION": str(HOOK_VERSION),
        "DISABLE_COMMAND": disable_command,
        "INSTANCE": shlex.quote(str(instance)),
        "PLAY_ARGS": words(play_args),
        "OPEN_ARGS": words(open_args),
        "MARKER": MARKER,
    }.items():
        text = text.replace(f"@@{key}@@", value)
    return text


def _is_ours(entry: object) -> bool:
    return isinstance(entry, dict) and MARKER in str(entry.get("args", "")).split()


def _load_settings(game: GogGame) -> tuple[dict, dict]:
    path = _settings_path(game)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HookError("Heroic has no settings for the game yet. Launch it once from Heroic first.") from None
    except (OSError, ValueError) as e:
        raise HookError(f"Heroic's settings for the game ({path}) can't be read: {e}") from None
    settings = data.get(game.app_name) if isinstance(data, dict) else None
    if not isinstance(settings, dict):
        raise HookError(f"Heroic's settings for the game ({path}) aren't in the form ModSync knows.")
    return data, settings


def _write(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, path)


def heroic_running() -> bool:
    """Heroic's own process: the distribution packages' Electron app, the
    official build in /opt, the AppImage or the Flatpak."""
    pattern = r"heroic-games-launcher/app\.asar|/opt/Heroic/heroic|/\.mount_Heroic|/app/bin/heroic/heroic"
    try:
        return background.run_host(["pgrep", "-f", pattern], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def status(game: GogGame, expected: str | None = None) -> HookStatus:
    """``expected`` is the script as :func:`render` would write it now."""
    script = script_path(game)
    try:
        _, settings = _load_settings(game)
    except HookError:
        return HookStatus(False, False, script)
    enabled = any(_is_ours(e) for e in settings.get("wrapperOptions") or [])
    try:
        current = expected is None or script.read_text(encoding="utf-8") == expected
    except OSError:
        current = False
    return HookStatus(enabled, current and script.is_file(), script)


def flatpak_sees(path: Path) -> bool:
    home = Path.home()
    return any(path.is_relative_to(home / v if not v.startswith("/") else Path(v)) for v in FLATPAK_VISIBLE)


def grant_flatpak_access(path: Path) -> bool:
    """Let Heroic's Flatpak read and write ``path``. False when it already could."""
    if flatpak_sees(path):
        return False
    result = background.run_host(["flatpak", "override", "--user", f"--filesystem={path}", FLATPAK_ID], timeout=30)
    if result.returncode != 0:
        raise HookError(f"Heroic's Flatpak can't see {path}, and giving it access failed: "
                        f"{(result.stderr or result.stdout).strip() or result.returncode}")
    return True


def enable(game: GogGame, script_text: str, instance: Path) -> list[str]:
    """Write the script and put it first among the game's wrappers. Returns
    what was done, for the user."""
    if heroic_running():
        raise HookError("Close Heroic first: it writes the game's settings back from memory while it runs.")
    data, settings = _load_settings(game)
    done: list[str] = []
    if game.flatpak and grant_flatpak_access(instance):
        done.append(f"Gave Heroic's Flatpak access to {instance}.")
    script = script_path(game)
    _write(script, script_text, mode=0o755)
    wrappers = [e for e in settings.get("wrapperOptions") or [] if not _is_ours(e)]
    settings["wrapperOptions"] = [{"exe": str(script), "args": f"{MARKER} {game.app_name}"}, *wrappers]
    _write(_settings_path(game), json.dumps(data, indent=2))
    done.append("Heroic's Play button on the game now goes through Mod Organizer 2.")
    return done


def disable(game: GogGame) -> str:
    if heroic_running():
        raise HookError("Close Heroic first: it writes the game's settings back from memory while it runs.")
    try:
        data, settings = _load_settings(game)
    except HookError:
        data = settings = None
    if settings is not None and any(_is_ours(e) for e in settings.get("wrapperOptions") or []):
        settings["wrapperOptions"] = [e for e in settings["wrapperOptions"] if not _is_ours(e)]
        _write(_settings_path(game), json.dumps(data, indent=2))
    script_path(game).unlink(missing_ok=True)
    return "Heroic's Play button on the game starts it as before."


def refresh(game: GogGame, script_text: str) -> bool:
    """Rewrite the script of an enabled hook that's out of date. Safe while
    Heroic runs; True when it changed."""
    state = status(game, script_text)
    if not state.enabled or state.current:
        return False
    _write(state.script, script_text, mode=0o755)
    return True
