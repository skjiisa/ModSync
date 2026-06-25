"""ModSync's own config/data locations (stdlib-only; no platformdirs dependency)."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "modsync"


def _xdg(env: str, default_rel: str) -> Path:
    base = os.environ.get(env)
    if not base:
        base = str(Path.home() / default_rel)
    return Path(base) / APP_NAME


def config_dir() -> Path:
    """Where ModSync stores its own settings."""
    return _xdg("XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    """Where ModSync stores its data (e.g. the dedicated Syncthing home)."""
    return _xdg("XDG_DATA_HOME", ".local/share")


def syncthing_home() -> Path:
    """Dedicated Syncthing home so we never collide with a user's own Syncthing."""
    return data_dir() / "syncthing"
