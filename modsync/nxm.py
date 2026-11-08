"""Nexus Mods' "Mod Manager Download" links (``nxm://``) for the games ModSync runs.

A link names its game first (``nxm://cyberpunk2077/mods/2987/files/168466?key=…``).
ModSync registers itself as the desktop's handler for ``nxm://`` and takes the
links of the games whose Mod Organizer 2 it starts, so a download reaches MO2
the way ModSync runs it (see each game's ``download``). Every other link goes on,
unchanged, to the handler that was there before: MO2-LINT's, usually, which
still serves the games ModSync doesn't run.

MO2-LINT's handler can't serve a game behind one of ModSync's Play hooks: with
MO2 closed it starts MO2 by launching the game from its store, which under the
hook plays the game, and it finds a running MO2 by a ``Z:`` path that umu's
drive letters don't give.

The handler is a desktop entry of its own (:data:`DESKTOP_ID`) in the host's
``~/.local/share/applications``, made default with ``xdg-mime``, as MO2-LINT's
is. The previous default is recorded in ``nxm.json`` and put back on disable.
Installing or updating MO2-LINT makes its handler the default again;
:func:`refresh` takes the links back from it, and only from it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from modsync import background, config

log = logging.getLogger(__name__)

SCHEME = "x-scheme-handler/nxm"
DESKTOP_ID = "modsync-nxm-handler.desktop"
# MO2-LINT's handler: today's, and the shell installer's before version 6.
MO2LINT_IDS = ("mo2lint_nxm-handler.desktop", "modorganizer2-nxm-handler.desktop")
_DESKTOP_ID_RE = re.compile(r"[A-Za-z0-9_.+-]+\.desktop")


class NxmError(RuntimeError):
    pass


@dataclass(frozen=True)
class GameHandler:
    name: str
    claims: Callable[[], bool]  # whether ModSync runs this game's MO2 here
    download: Callable[[str], str]  # hands the link to MO2; returns what happened


def _cyberpunk() -> GameHandler:
    from modsync import cyberpunk

    return GameHandler(cyberpunk.CYBERPUNK.name, cyberpunk.takes_downloads, cyberpunk.download)


# Keyed by the game in the link: Nexus' name for it (Game.nexus_slug).
HANDLERS: dict[str, Callable[[], GameHandler]] = {"cyberpunk2077": _cyberpunk}


def game_of(url: str) -> str | None:
    """The game a link is for, or None when it isn't an nxm:// link."""
    match = re.match(r"nxm://([^/?#]+)", url.strip(), re.IGNORECASE)
    return match[1].lower() if match else None


# --- the desktop's handler ------------------------------------------------------


def applications_dir() -> Path:
    """The folder for the user's own desktop entries, as the host sets it
    (``$XDG_DATA_HOME/applications``). Asked of the host: the Flatpak's
    XDG_DATA_HOME is its own."""
    try:
        result = background.run_host(["sh", "-c", 'printf %s "${XDG_DATA_HOME:-}"'], timeout=10)
        data = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        data = ""
    return (Path(data) if data.startswith("/") else Path.home() / ".local" / "share") / "applications"


def entry_path() -> Path:
    return applications_dir() / DESKTOP_ID


def _settings_path() -> Path:
    return config.config_dir() / "nxm.json"


def _settings() -> dict:
    try:
        data = json.loads(_settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def exec_quote(arg: str) -> str:
    """One argument of a desktop entry's Exec line, per the Desktop Entry spec:
    double-quoted with ``" ` $ \\`` escaped when it needs quoting, then the
    string escape that doubles every backslash again, and ``%`` doubled."""
    if arg and not re.search(r"""[\s"'\\><~|&;$*?#()`]""", arg):
        quoted = arg
    else:
        quoted = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")


def render_entry(command: list[str]) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=ModSync Nexus downloads",
        "Comment=Send Nexus Mods downloads to Mod Organizer 2 the way ModSync runs it",
        "Icon=io.github.skjiisa.ModSync",
        "Exec=" + " ".join([*(exec_quote(a) for a in command), "nxm", "%u"]),
        f"MimeType={SCHEME};",
        "NoDisplay=true",
        "Terminal=false",
        "",
    ])


def default_handler() -> str | None:
    """The desktop entry handling nxm:// links now, or None for none."""
    try:
        result = background.run_host(["xdg-mime", "query", "default", SCHEME], timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise NxmError(f"Couldn't ask xdg-mime which program opens nxm:// links: {e}") from e
    if result.returncode != 0:
        raise NxmError("Couldn't ask xdg-mime which program opens nxm:// links: "
                       f"{(result.stderr or '').strip() or result.returncode}")
    return result.stdout.strip() or None


def _make_default(desktop_id: str) -> None:
    try:
        result = background.run_host(["xdg-mime", "default", desktop_id, SCHEME], timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise NxmError(f"xdg-mime couldn't make {desktop_id} open nxm:// links: {e}") from e
    if result.returncode != 0:
        raise NxmError(f"xdg-mime couldn't make {desktop_id} open nxm:// links: "
                       f"{(result.stderr or '').strip() or result.returncode}")


def _update_database() -> None:
    """Refresh the desktop's cache of which entry opens what. Best effort:
    ``xdg-mime default`` alone is enough for the default."""
    try:
        background.run_host(["update-desktop-database", str(applications_dir())], timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


@dataclass(frozen=True)
class Status:
    enabled: bool  # ModSync set itself up as the handler (and wasn't disabled)
    default: str | None  # what opens nxm:// links now
    previous: str | None  # what did before ModSync, which gets the other games' links

    @property
    def active(self) -> bool:
        return self.enabled and self.default == DESKTOP_ID

    def summary(self) -> str:
        if not self.enabled:
            return f"off ({self.default or 'nothing'} opens them)"
        if not self.active:
            return f"on, but {self.default or 'nothing'} opens them now ('modsync nxm enable' takes them back)"
        return "on" + (f"; other games' links go to {self.previous}" if self.previous else "")


def status() -> Status:
    settings = _settings()
    try:
        default = default_handler()
    except NxmError:
        default = None
    enabled = bool(settings.get("enabled")) and entry_path().is_file()
    previous = settings.get("previous")
    return Status(enabled, default, previous if isinstance(previous, str) else None)


def _command(settings: dict) -> list[str]:
    """How the handler starts ModSync: the copy it already starts, while that
    still works, as for Steam's launch hook (launchhook._hook_command)."""
    from modsync import launchhook

    here = launchhook.modsync_command()
    recorded = settings.get("command")
    if config.in_flatpak() or not isinstance(recorded, list) or not recorded or not launchhook._runnable(recorded):
        return here
    return recorded


def enable() -> str:
    """Make ModSync the handler for nxm:// links. Returns what happened."""
    settings = _settings()
    current = default_handler()
    previous = settings.get("previous") if current == DESKTOP_ID else current
    command = _command(settings)
    _write(entry_path(), render_entry(command))
    _make_default(DESKTOP_ID)
    _update_database()
    _write(_settings_path(), json.dumps({"enabled": True, "previous": previous, "command": command}, indent=2))
    other = f" Other games' links still go to {previous}." if previous else ""
    return f"Nexus Mods downloads for the games ModSync runs now go to their Mod Organizer 2.{other}"


def disable() -> str:
    """Hand nxm:// links back to the handler from before ModSync. Nothing of
    ModSync's is removed until that has worked, so a failure can be retried."""
    previous = _settings().get("previous")
    previous = previous if isinstance(previous, str) and _DESKTOP_ID_RE.fullmatch(previous) else None
    message = "ModSync no longer opens nxm:// links."
    if default_handler() == DESKTOP_ID:
        if previous and entry_text(previous) is not None:
            _make_default(previous)
            message = f"nxm:// links go to {previous} again."
        elif previous:
            message += f" {previous}, which opened them before, isn't installed anymore."
    entry_path().unlink(missing_ok=True)
    _settings_path().unlink(missing_ok=True)
    _update_database()
    return message


def refresh() -> list[str]:
    """Keep an enabled handler in place: rewrite its entry if this ModSync
    starts differently, and take the links back from MO2-LINT's handler,
    which makes itself the default again whenever MO2-LINT installs. Any other
    program the user chose since is left alone. Never raises; returns what the
    user should know."""
    settings = _settings()
    if not settings.get("enabled"):
        return []
    try:
        command = _command(settings)
        text = render_entry(command)
        notes: list[str] = []
        try:
            current_text = entry_path().read_text(encoding="utf-8")
        except OSError:
            current_text = None
        if current_text != text:
            _write(entry_path(), text)
            if command != settings.get("command"):
                _write(_settings_path(), json.dumps({**settings, "command": command}, indent=2))
        current = default_handler()
        if current in MO2LINT_IDS:
            _make_default(DESKTOP_ID)
            _update_database()
            notes.append(f"Took Nexus Mods downloads back from {current}, which had made itself their handler again.")
        return notes
    except (OSError, NxmError) as e:
        log.warning("couldn't keep ModSync's nxm:// handler in place: %s", e)
        return []


# --- opening a link ---------------------------------------------------------------


# Prints the desktop entry with the id in $1 from the host's data folders, as
# the desktop looks it up (a foo-bar.desktop may be foo/bar.desktop). Run on
# the host: from inside the Flatpak, neither the host's /usr nor its
# XDG_DATA_DIRS are visible.
_PRINT_ENTRY = r"""
set -f
id=$1
nested=$(printf %s "$id" | sed 's|-|/|')
IFS=:
for d in ${XDG_DATA_HOME:-$HOME/.local/share} ${XDG_DATA_DIRS:-/usr/local/share:/usr/share} \
         $HOME/.local/share/flatpak/exports/share /var/lib/flatpak/exports/share; do
    for f in "$d/applications/$id" "$d/applications/$nested"; do
        if [ -f "$f" ]; then cat "$f"; exit 0; fi
    done
done
exit 1
"""


def entry_text(desktop_id: str) -> str | None:
    """The desktop entry with this id, as the host's desktop would find it;
    None when it isn't installed. Raises when the host couldn't be asked."""
    if not _DESKTOP_ID_RE.fullmatch(desktop_id):
        return None
    try:
        result = background.run_host(["sh", "-c", _PRINT_ENTRY, "sh", desktop_id], timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        raise NxmError(f"Couldn't look for {desktop_id}: {e}") from e
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        raise NxmError(f"Couldn't look for {desktop_id}: {(result.stderr or '').strip() or result.returncode}")
    return result.stdout if result.stdout.strip() else None


_DROPPED_CODES = {"%i", "%c", "%k", "%d", "%D", "%n", "%N", "%v", "%m"}


def _split_exec(line: str) -> list[str]:
    """An Exec value's arguments: double quotes with ``" ` $ \\`` escaped (the
    spec), single quotes and bare backslashes as GLib also takes them."""
    args: list[str] = []
    current: list[str] = []
    started = False
    i = 0
    while i < len(line):
        c = line[i]
        if c in " \t\n":
            if started:
                args.append("".join(current))
                current, started = [], False
        elif c == '"':
            started = True
            i += 1
            while i < len(line) and line[i] != '"':
                if line[i] == "\\" and i + 1 < len(line) and line[i + 1] in '"`$\\':
                    i += 1
                current.append(line[i])
                i += 1
        elif c == "'":
            started = True
            end = line.find("'", i + 1)
            end = len(line) if end < 0 else end
            current.append(line[i + 1:end])
            i = end
        elif c == "\\" and i + 1 < len(line):
            started = True
            i += 1
            current.append(line[i])
        else:
            started = True
            current.append(c)
        i += 1
    if started:
        args.append("".join(current))
    return args


def entry_command(entry: str, url: str) -> list[str]:
    """The command the desktop entry ``entry`` (its text) runs to open ``url``."""
    exec_line = None
    section = None
    for line in entry.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Desktop Entry" and line.startswith("Exec="):
            exec_line = line[len("Exec="):]
            break
    if not exec_line:
        raise NxmError("The desktop entry has no Exec line.")
    # The string escapes come first, then the quoting (the Desktop Entry spec).
    unescaped = re.sub(r"\\([sntr\\])", lambda m: {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[m[1]],
                       exec_line)
    argv: list[str] = []
    placed = False
    for arg in _split_exec(unescaped):
        if arg in _DROPPED_CODES:
            continue
        if arg in ("%u", "%U", "%f", "%F"):
            argv.append(url)
            placed = True
            continue
        if re.search(r"%[uUfF]", arg):
            placed = True
        argv.append(re.sub(r"%([uUfF%])", lambda m: url if m[1] != "%" else "%", arg))
    if not placed:
        argv.append(url)
    return argv


def forward(url: str) -> str:
    """Open a link with the handler ModSync replaced."""
    previous = _settings().get("previous")
    if not isinstance(previous, str) or not previous or previous == DESKTOP_ID:
        raise NxmError(f"ModSync doesn't run Mod Organizer 2 for '{game_of(url)}', "
                       "and no other program was set up to open nxm:// links.")
    entry = entry_text(previous)
    if entry is None:
        raise NxmError(f"{previous}, which opened nxm:// links before ModSync, isn't installed anymore.")
    argv = entry_command(entry, url)
    log.info("passing a %s link to %s", game_of(url), previous)
    if config.in_flatpak():
        argv = ["flatpak-spawn", "--host", *argv]
    env = {k: v for k, v in os.environ.items() if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT")}
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env)
    return f"Passed the link to {previous}."


def handle(url: str) -> str:
    """Open an nxm:// link: with the game's MO2 when ModSync runs it, else with
    the handler from before. Returns what happened; raises what went wrong."""
    game = game_of(url)
    if game is None or "\n" in url or "\r" in url:
        raise NxmError("That isn't an nxm:// link.")
    make = HANDLERS.get(game)
    handler = make() if make is not None else None
    if handler is None or not handler.claims():
        return forward(url)
    log.info("downloading a %s file from Nexus Mods", handler.name)
    return handler.download(url)
