"""Open ModSync when Skyrim is launched from Steam — the *launch hook*.

Pressing Play runs whatever compatibility tool Steam has selected for the game.
The hook is one more such tool, registered under ``compatibilitytools.d`` as
``modsync_<appid>_hub`` (the approach MO2-LINT took in PR #1096 for its own
``mo2_<appid>_redirector``): a ``proton`` script that Steam calls in place of
Proton. Ours opens the ModSync hub window and then hands the **same** launch on
to the tool Steam used before — MO2-LINT's redirector when it is installed
(Play → ModSync → Mod Organizer 2), otherwise the plain Proton — inside the
Steam Linux Runtime container that tool asks for. Cancel in the hub ends the
launch with a clean exit, so Steam just returns to the library.

Two facts make it survive updates. Nothing of MO2-LINT's is copied: the chain
refers to its tool directory by path and re-reads its ``toolmanifest.vdf`` on
every launch, so a reinstalled or upgraded redirector (new Proton, new
runtime) is picked up as-is. And the hub is started through a stable command
(``flatpak run io.github.skjiisa.ModSync`` or the installed ``modsync``), never
a versioned path.

Steam's own choice is recorded before it is changed and written back on
disable. ``config.vdf`` can only be edited while Steam is closed, so with Steam
running the change is queued and applied by the app or the background service
the moment Steam exits — the same mechanism the appmanifest pin uses.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from modsync import __version__, config, platforms
from modsync.games import GAMES, SKYRIM_SE, Game
from modsync.steam import compattools, shortcuts
from modsync.steam import libraries as libs
from modsync.steam.compattools import CompatTool
from modsync.steam.steamconfig import SteamConfig, config_vdf_path

log = logging.getLogger(__name__)

TEMPLATE_VERSION = 1
TEMPLATE_DIR = Path(__file__).parent / "steam" / "launchhook_template"
MARKER = ".modsync-launch-hook"  # only directories carrying this are ever removed

EXIT_CONTINUE = 0
EXIT_CANCEL = 10

_TOOL_ID_RE = re.compile(r"^modsync_(\d+)_hub$")


def tool_id(appid: int) -> str:
    return f"modsync_{appid}_hub"


def display_name(game: Game) -> str:
    return f"ModSync ({game.name})"


def modsync_command() -> list[str]:
    """How the hook starts ModSync on the host: the Flatpak when running as one,
    otherwise the installed command (or this interpreter)."""
    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id or Path("/.flatpak-info").exists():
        return ["/usr/bin/flatpak", "run", flatpak_id or shortcuts.MODSYNC_FLATPAK_ID]
    exe = shutil.which("modsync")
    if exe:
        return [exe]
    return [sys.executable, "-m", "modsync"]


# --- what ModSync remembers -----------------------------------------------------


@dataclass
class Record:
    """Written on enable, removed on disable: what was chained to, and what
    Steam had selected before so it can be put back exactly."""

    appid: int
    tool_id: str
    tool_path: str
    underlying_name: str
    underlying_path: str
    underlying_display: str
    previous: dict | None  # Steam's CompatToolMapping entry before enabling; None = no explicit choice
    command: list[str] = field(default_factory=list)
    template_version: int = TEMPLATE_VERSION
    enabled_at: str = ""

    @staticmethod
    def path() -> Path:
        return config.config_dir() / "launch-hook.json"

    @classmethod
    def load(cls) -> "Record | None":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
            return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        except (OSError, ValueError, TypeError):
            return None

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def remove(cls) -> None:
        cls.path().unlink(missing_ok=True)

    @classmethod
    def from_marker(cls, tool_dir: Path) -> "Record | None":
        """The same record as written into the tool directory's marker file —
        the copy that another ModSync install (Flatpak vs. native, each with its
        own config dir) can still read."""
        try:
            data = json.loads((tool_dir / MARKER).read_text(encoding="utf-8"))
            record = data.get("record")
            if not isinstance(record, dict):
                return None
            return cls(**{k: record[k] for k in cls.__dataclass_fields__ if k in record})
        except (OSError, ValueError, TypeError):
            return None

    def write_marker(self) -> None:
        marker = Path(self.tool_path) / MARKER
        marker.write_text(
            json.dumps({"modsync": __version__, "template_version": TEMPLATE_VERSION, "record": asdict(self)}, indent=2)
            + "\n",
            encoding="utf-8",
        )


def load_record(tool_dir: Path | None) -> "Record | None":
    """ModSync's own record, else the copy inside the tool directory."""
    record = Record.load()
    if record is None and tool_dir is not None:
        record = Record.from_marker(tool_dir)
    return record


@dataclass
class Pending:
    """A config.vdf change waiting for Steam to be closed."""

    action: str  # "select" (make Steam use the hook) | "restore" (put the previous tool back)
    appid: int
    mapping: dict | None  # the entry to write; None = remove the entry
    remove_tool: bool = False
    requested_at: str = ""

    @staticmethod
    def path() -> Path:
        return config.config_dir() / "pending-launch.json"

    @classmethod
    def load(cls) -> "Pending | None":
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
            return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        except (OSError, ValueError, TypeError):
            return None

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not self.requested_at:
            self.requested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        p.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def clear(cls) -> None:
        cls.path().unlink(missing_ok=True)


# --- Steam on this machine --------------------------------------------------------


@dataclass
class SteamEnv:
    root: Path
    libraries: list[libs.Library]

    @property
    def config_vdf(self) -> Path:
        return config_vdf_path(self.root)

    def tool_dir(self, appid: int) -> Path:
        return compattools.compatibilitytools_dir(self.root) / tool_id(appid)


def steam_env() -> SteamEnv | None:
    roots = platforms.current().steam_roots()
    if not roots:
        return None
    root = next((r for r in roots if config_vdf_path(r).exists()), roots[0])
    return SteamEnv(root, libs.all_libraries(roots))


def is_hook_dir(path: Path) -> bool:
    return (path / MARKER).exists()


# --- status -------------------------------------------------------------------------


@dataclass
class LaunchHookStatus:
    game: Game
    installed: bool  # the tool directory exists (with our marker)
    current_mapping: str | None  # what Steam launches the game with right now
    selected: bool  # …and that is the hook
    underlying_name: str | None
    underlying_display: str | None
    underlying_exists: bool
    pending: Pending | None
    steam_running: bool
    steam_found: bool
    outdated: bool = False

    @property
    def enabled(self) -> bool:
        return self.installed and self.selected

    @property
    def hands_off_to(self) -> str:
        """What Continue in the hub leads to."""
        return describe_target(self.underlying_name, self.underlying_display)

    @property
    def continue_label(self) -> str:
        return continue_label(self.underlying_name)

    def summary(self) -> str:
        g = self.game.name
        if not self.steam_found:
            return "Steam was not found on this machine."
        if self.pending and self.pending.action == "select":
            return (
                f"Waiting for Steam to be closed to select ModSync for {g}. Restart Steam; "
                "ModSync (or its background service) makes the switch while Steam is down."
            )
        if self.pending and self.pending.action == "restore":
            return "Waiting for Steam to be closed to restore the previous launcher."
        if self.enabled:
            if not self.underlying_exists:
                return (
                    f"On, but the tool it hands off to ({self.underlying_display}) is missing, so "
                    f"{g} will not start. Turn the hook off or on again."
                )
            return f"On. Steam's Play button opens ModSync, then continues to {self.hands_off_to}."
        if self.installed:
            via = f" with {self.current_mapping}" if self.current_mapping else ""
            return (
                f"Installed, but Steam still launches {g}{via}. Pick \"{display_name(self.game)}\" under "
                "Properties, then Compatibility in Steam, or turn the hook on again."
            )
        if self.selected:
            return (
                f"Steam is set to launch {g} through ModSync, but the hook files are gone. "
                "Turn it off to restore the previous launcher, or on to reinstall."
            )
        return f"Off. Steam's Play button starts {g} directly."


def describe_target(underlying_name: str | None, underlying_display: str | None = None) -> str:
    if underlying_name and compattools.MO2LINT_TOOL_RE.match(underlying_name):
        return "Mod Organizer 2 (MO2-LINT)"
    return f"the game ({underlying_display or underlying_name})" if underlying_name else "the game"


def continue_label(underlying_name: str | None, game: Game = SKYRIM_SE) -> str:
    if underlying_name and compattools.MO2LINT_TOOL_RE.match(underlying_name):
        return "Continue to Mod Organizer"
    return f"Continue to {game.name}"


def status(appid: int = SKYRIM_SE.appid) -> LaunchHookStatus:
    game = GAMES.get(appid, SKYRIM_SE)
    env = steam_env()
    pending = Pending.load()
    if pending and pending.appid != appid:
        pending = None
    running = shortcuts.steam_is_running()
    if env is None:
        return LaunchHookStatus(game, False, None, False, None, None, False, pending, running, False)
    tool_dir = env.tool_dir(appid)
    record = load_record(tool_dir)
    installed = is_hook_dir(tool_dir) and (tool_dir / "proton").exists()
    current: str | None = None
    try:
        current = SteamConfig.load(env.config_vdf).compat_tool_name(appid)
    except (OSError, ValueError):
        current = None
    underlying_name = record.underlying_name if record else None
    underlying_display = record.underlying_display if record else None
    underlying_exists = bool(record and Path(record.underlying_path, "proton").exists())
    outdated = bool(record and installed and record.template_version != TEMPLATE_VERSION)
    return LaunchHookStatus(
        game=game,
        installed=installed,
        current_mapping=current,
        selected=current == tool_id(appid),
        underlying_name=underlying_name,
        underlying_display=underlying_display,
        underlying_exists=underlying_exists,
        pending=pending,
        steam_running=running,
        steam_found=True,
        outdated=outdated,
    )


# --- rendering ----------------------------------------------------------------------


def _bash_array(items: list[str]) -> str:
    return " ".join(shlex.quote(str(i)) for i in items)


def render(
    target: Path,
    *,
    game: Game,
    underlying: CompatTool,
    command: list[str],
    library_paths: list[Path],
    template_dir: Path = TEMPLATE_DIR,
) -> None:
    """Write the tool directory. Refuses to touch a directory that exists but
    is not ours (no marker), so a corrupted record can never delete a real Proton."""
    if target.exists() and not is_hook_dir(target):
        raise RuntimeError(f"{target} exists but is not a ModSync launch hook; not overwriting it")
    replacements = {
        "@@TOOL_ID@@": tool_id(game.appid),
        "@@DISPLAY_NAME@@": display_name(game),
        "@@APPID@@": str(game.appid),
        "@@GAME_NAME@@": game.name,
        "@@MODSYNC_VERSION@@": __version__,
        "@@UNDERLYING_NAME@@": underlying.name,
        "@@UNDERLYING_DISPLAY@@": underlying.display_name,
        "@@UNDERLYING_PATH@@": str(underlying.path),
        "@@MODSYNC_COMMAND@@": _bash_array(command),
        "@@LIBRARY_PATHS@@": _bash_array([str(p) for p in library_paths]),
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / MARKER).write_text(
        json.dumps({"modsync": __version__, "template_version": TEMPLATE_VERSION}) + "\n",
        encoding="utf-8",
    )
    for source in sorted(template_dir.iterdir()):
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        for key, value in replacements.items():
            text = text.replace(key, value)
        dest = target / source.name
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(dest)
    proton = target / "proton"
    proton.chmod(proton.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def remove_tool_dir(path: Path) -> bool:
    if not path.exists():
        return False
    if not is_hook_dir(path):
        raise RuntimeError(f"{path} carries no ModSync marker; not removing it")
    shutil.rmtree(path)
    return True


# --- enable / disable -------------------------------------------------------------


def game_proton(appid: int = SKYRIM_SE.appid, env: SteamEnv | None = None) -> CompatTool | None:
    """The real Proton Steam would launch the game with right now, looking
    through ModSync's hook to the tool it hands off to. ``None`` when Steam has
    no usable choice recorded (no mapping, or a tool such as MO2-LINT's
    redirector that is not itself a Proton)."""
    env = env or steam_env()
    if env is None or not env.config_vdf.exists():
        return None
    try:
        current = SteamConfig.load(env.config_vdf).compat_tool_name(appid)
    except (OSError, ValueError):
        return None
    if not current:
        return None
    if current == tool_id(appid):
        record = load_record(env.tool_dir(appid))
        current = record.underlying_name if record else None
        if not current:
            return None
    if _TOOL_ID_RE.match(current) or compattools.MO2LINT_TOOL_RE.match(current):
        return None
    tool = compattools.find_tool(current, env.root, env.libraries)
    if tool is None or not (tool.path / "proton").is_file():
        return None
    return tool


def _resolve_underlying(
    env: SteamEnv, appid: int, current: str | None, record: Record | None, through: str | None
) -> CompatTool:
    ours = tool_id(appid)
    if through:
        tool = compattools.find_tool(through, env.root, env.libraries)
        if tool is None:
            raise RuntimeError(f"no compatibility tool named {through!r} is installed")
    elif (mo2 := compattools.mo2lint_tool(appid, env.root)) is not None:
        tool = mo2  # MO2-LINT wired Mod Organizer into this game: keep that
    elif current and current != ours:
        tool = compattools.find_tool(current, env.root, env.libraries)
        if tool is None:
            raise RuntimeError(
                f"Steam launches the game with {current!r}, but that tool was not found on this machine"
            )
    elif current == ours and record:
        tool = compattools.find_tool(record.underlying_name, env.root, env.libraries)
        if tool is None:
            raise RuntimeError(
                f"the tool the hook hands off to ({record.underlying_display}) is no longer installed; "
                "pick a Proton for the game in Steam, then turn the hook on again"
            )
    else:
        default = SteamConfig.load(env.config_vdf).compat_tool_name(0) if env.config_vdf.exists() else None
        tool = compattools.find_tool(default, env.root, env.libraries) if default else None
        if tool is None:
            tool = compattools.default_valve_tool(env.root, env.libraries)
        if tool is None:
            raise RuntimeError(
                "could not tell which Proton Steam uses for the game; pick one under "
                "Properties, then Compatibility in Steam, and try again"
            )
    if tool.name == ours or _TOOL_ID_RE.match(tool.name):
        raise RuntimeError("the hook cannot hand off to itself")
    if not (tool.path / "proton").exists():
        raise RuntimeError(f"{tool.display_name} at {tool.path} has no 'proton' to run")
    return tool


def enable(appid: int = SKYRIM_SE.appid, *, through: str | None = None) -> str:
    """Install the hook for the game and make Steam use it. Returns what happened."""
    game = GAMES.get(appid, SKYRIM_SE)
    env = steam_env()
    if env is None:
        raise RuntimeError("Steam was not found on this machine")
    if not env.config_vdf.exists():
        raise RuntimeError(f"{env.config_vdf} not found. Has Steam been run on this machine?")
    cfg = SteamConfig.load(env.config_vdf)
    current_entry = cfg.compat_tool(appid)
    current = str(current_entry["name"]) if current_entry and current_entry.get("name") else None
    ours = tool_id(appid)
    record = load_record(env.tool_dir(appid))

    underlying = _resolve_underlying(env, appid, current, record, through)
    # Keep the very first "previous" across re-enables, so disable restores what
    # the user had before ModSync ever touched it.
    previous = record.previous if (record and current == ours) else current_entry

    target = env.tool_dir(appid)
    render(
        target,
        game=game,
        underlying=underlying,
        command=modsync_command(),
        library_paths=[lib.path for lib in env.libraries],
    )
    new_record = Record(
        appid=appid,
        tool_id=ours,
        tool_path=str(target),
        underlying_name=underlying.name,
        underlying_path=str(underlying.path),
        underlying_display=underlying.display_name,
        previous=previous,
        command=modsync_command(),
        enabled_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    new_record.save()
    new_record.write_marker()

    hands = describe_target(underlying.name, underlying.display_name)
    log.info("launch hook enabled for %s: hands off to %s (%s); Steam had %s; steam running: %s",
             appid, underlying.name, underlying.path, current or "(default)", shortcuts.steam_is_running())
    if current == ours:
        Pending.clear()
        return f"Launch hook refreshed. Play opens ModSync, then continues to {hands}."
    if shortcuts.steam_is_running():
        Pending("select", appid, {"name": ours, "config": "", "priority": "250"}).save()
        return (
            f"Launch hook installed; it continues to {hands}. Steam is running, so the switch is "
            "queued. Restart Steam (on the Deck: Power, then Restart Steam) and ModSync, either the open app "
            "or its background service, selects the hook while Steam is closed. You can also pick "
            f"\"{display_name(game)}\" yourself under Properties, then Compatibility."
        )
    cfg.set_compat_tool(appid, ours)
    cfg.save()
    Pending.clear()
    return f"Launch hook enabled; it continues to {hands}. Start Steam and press Play on {game.name}."


def disable(appid: int = SKYRIM_SE.appid) -> str:
    """Give Steam back its previous choice and remove the hook."""
    game = GAMES.get(appid, SKYRIM_SE)
    env = steam_env()
    record = load_record(env.tool_dir(appid) if env else None)
    previous = record.previous if record else None
    log.info("launch hook disable for %s: restoring %s", appid, (previous or {}).get("name") or "Steam's default")
    if env is None:
        Record.remove()
        Pending.clear()
        return "Steam was not found; forgot the launch hook."
    target = env.tool_dir(appid)
    current = None
    if env.config_vdf.exists():
        current = SteamConfig.load(env.config_vdf).compat_tool_name(appid)
    ours = tool_id(appid)

    if current == ours:
        if shortcuts.steam_is_running():
            Pending("restore", appid, previous, remove_tool=True).save()
            return (
                "Steam is running, so restoring the previous launcher is queued: it happens the "
                "moment Steam is closed (ModSync or its background service must be running). "
                f"Until then Play still opens ModSync for {game.name}."
            )
        cfg = SteamConfig.load(env.config_vdf)
        cfg.set_compat_entry(appid, previous)
        cfg.save()
    remove_tool_dir(target)
    Record.remove()
    Pending.clear()
    back = f"back to {previous['name']}" if previous and previous.get("name") else "back to Steam's default"
    return f"Launch hook off. {game.name} launches {back}."


def apply_pending() -> str | None:
    """Apply a queued config.vdf change once Steam has exited (called from the
    serve loop and the dashboard). Steam rewrites its files on shutdown, so
    wait a moment after it disappears before touching them."""
    pending = Pending.load()
    if pending is None or shortcuts.steam_is_running():
        return None
    time.sleep(2.0)
    if shortcuts.steam_is_running():
        return None
    env = steam_env()
    if env is None or not env.config_vdf.exists():
        return None
    log.info("applying queued launch hook change: %s for %s", pending.action, pending.appid)
    try:
        cfg = SteamConfig.load(env.config_vdf)
        if pending.action == "select":
            if not is_hook_dir(env.tool_dir(pending.appid)):
                Pending.clear()
                return "Launch hook: the tool files are gone, so the queued switch was dropped."
            cfg.set_compat_entry(pending.appid, pending.mapping)
            cfg.save()
            Pending.clear()
            return "Launch hook selected for the game. Play now opens ModSync first."
        cfg.set_compat_entry(pending.appid, pending.mapping)
        cfg.save()
        if pending.remove_tool:
            remove_tool_dir(env.tool_dir(pending.appid))
            Record.remove()
        Pending.clear()
        name = (pending.mapping or {}).get("name")
        return f"Launch hook off. The game launches {'with ' + str(name) if name else 'with Steam\'s default'} again."
    except Exception as exc:  # keep the loop alive; the queue stays for the next try
        log.error("queued launch hook change failed: %s", exc)
        return f"Launch hook: could not apply the queued change: {exc}"


def refresh_if_outdated(appid: int = SKYRIM_SE.appid) -> bool:
    """Re-render the hook after a ModSync update changed the template. Only our
    own marker-carrying directory is touched, and only when a record exists."""
    env = steam_env()
    if env is None:
        return False
    target = env.tool_dir(appid)
    record = load_record(target)
    if record is None or record.appid != appid:
        return False
    if not is_hook_dir(target) or record.template_version == TEMPLATE_VERSION:
        return False
    underlying = compattools.find_tool(record.underlying_name, env.root, env.libraries)
    if underlying is None:
        return False
    command = record.command if record.command and Path(record.command[0]).exists() else modsync_command()
    render(
        target,
        game=GAMES.get(appid, SKYRIM_SE),
        underlying=underlying,
        command=command,
        library_paths=[lib.path for lib in env.libraries],
    )
    record.template_version = TEMPLATE_VERSION
    record.command = command
    record.save()
    record.write_marker()
    return True
