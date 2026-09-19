"""File logging for every way ModSync runs (GUI, CLI, ``serve``, the launch hub).

Everything goes to one rotating file, ``$XDG_STATE_HOME/modsync/modsync.log``
(default ``~/.local/state/modsync``, next to the launch hook's own
``launch-hook.log``), so a bug report from a Steam Deck can carry the whole
story: ``modsync diagnostics`` collects it. Warnings and errors also reach
stderr. ``MODSYNC_LOG_LEVEL`` (e.g. ``DEBUG``) overrides the default ``INFO``.

Stdlib-only, like ``doctor``: ``platformdirs`` is used when installed, and the
XDG fallback gives the same directory on Linux. Configuring never raises — if
the directory cannot be written, logging simply stays on stderr.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from modsync import __version__
from modsync.config import APP_NAME
from modsync.diagnostics import redact

LOG_FILE_NAME = "modsync.log"
LAUNCH_HOOK_LOG_NAME = "launch-hook.log"
MAX_BYTES = 1_000_000
BACKUP_COUNT = 3

_MARKER = "_modsync_handler"  # set on handlers we own, so reconfiguring replaces only ours


def state_dir() -> Path:
    """``~/.local/state/modsync`` (or ``$XDG_STATE_HOME/modsync``)."""
    try:
        from platformdirs import user_state_dir

        return Path(user_state_dir(APP_NAME))
    except Exception:  # not installed, or a broken environment: same answer by hand
        base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
        return Path(base) / APP_NAME


def log_path() -> Path:
    return state_dir() / LOG_FILE_NAME


def launch_hook_log_path() -> Path:
    """Where the shell launch hook writes (see steam/launchhook_template/proton)."""
    return state_dir() / LAUNCH_HOOK_LOG_NAME


class _Redacting(logging.Formatter):
    """Last line of defence: no pairing code / API key / full device id reaches a log."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def _level_from_env() -> int:
    name = (os.environ.get("MODSYNC_LOG_LEVEL") or "INFO").strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def configure(component: str, argv: list[str] | None = None) -> Path | None:
    """Set up the root logger for this process. ``component`` is one of
    ``gui`` / ``cli`` / ``serve`` / ``hub`` and appears in every line. Returns
    the log file path, or None when only stderr could be set up."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _MARKER, False):
            root.removeHandler(handler)
            handler.close()

    level = _level_from_env()
    root.setLevel(min(level, logging.INFO))
    fmt = _Redacting(
        f"%(asctime)s %(levelname)-7s [{component}] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setLevel(max(level, logging.WARNING))
    stderr.setFormatter(fmt)
    setattr(stderr, _MARKER, True)
    root.addHandler(stderr)

    path: Path | None = None
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        setattr(file_handler, _MARKER, True)
        root.addHandler(file_handler)
    except Exception as exc:  # unwritable HOME, read-only sandbox, …: stderr only
        path = None
        logging.getLogger(__name__).warning("cannot write the log file (%s); logging to stderr only", exc)

    # Third-party chatter (httpx logs every request at INFO) would drown ours.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    shown = list(sys.argv[1:]) if argv is None else list(argv)
    logging.getLogger("modsync").info(
        "ModSync %s starting (%s, argv=%s)", __version__, component, redact(" ".join(shown)) or "-"
    )
    return path
