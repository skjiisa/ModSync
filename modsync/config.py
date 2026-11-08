"""ModSync's own config/data locations (stdlib-only; no platformdirs dependency).

Every copy of ModSync on a machine uses the same ones, so a Flatpak and a
copy run from source (or installed with pip) never end up as two machines
with two pairings. Where they are, first match wins:

1. ``$MODSYNC_HOME/config`` and ``$MODSYNC_HOME/data``: a separate set, for
   development (``MODSYNC_HOME=~/modsync-dev uv run modsync``).
2. ``$XDG_CONFIG_HOME/modsync`` and ``$XDG_DATA_HOME/modsync`` when they're
   set to somewhere other than the standard place. Inside the Flatpak they
   point under ``~/.var/app``, which is how the Flatpak gets its own.
3. Outside the Flatpak, while it's installed (or has left its data behind),
   the Flatpak's: ``~/.var/app/io.github.skjiisa.ModSync/{config,data}/modsync``.
   Not for a copy with a setup of its own in ``~/.config/modsync``, though:
   switching would leave its pairing behind, or bring back an old one the
   Flatpak kept. :func:`other_setup` points out the two.
4. ``~/.config/modsync`` and ``~/.local/share/modsync``.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "modsync"
FLATPAK_APP_ID = "io.github.skjiisa.ModSync"
SYSTEM_FLATPAK = Path("/var/lib/flatpak")


def in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def flatpak_home() -> Path:
    """The Flatpak's folder under ``~/.var/app``, where its XDG folders point."""
    return Path.home() / ".var" / "app" / FLATPAK_APP_ID


def flatpak_installed() -> bool:
    return any((base / "app" / FLATPAK_APP_ID).is_dir()
               for base in (Path.home() / ".local" / "share" / "flatpak", SYSTEM_FLATPAK))


def _standard_config() -> Path:
    return Path.home() / ".config" / APP_NAME


def shares_flatpak_data() -> bool:
    """Whether this copy, run outside the Flatpak, uses the Flatpak's folders."""
    if in_flatpak() or (_standard_config() / "state.json").is_file():
        return False
    return flatpak_installed() or (flatpak_home() / "config" / APP_NAME).is_dir()


def _dir(env: str, standard_rel: str, kind: str) -> Path:
    home = os.environ.get("MODSYNC_HOME")
    if home:
        return Path(home).expanduser() / kind
    standard = Path.home() / standard_rel
    base = os.environ.get(env)
    if base and Path(base).expanduser() != standard:
        return Path(base) / APP_NAME
    if shares_flatpak_data():
        return flatpak_home() / kind / APP_NAME
    return standard / APP_NAME


def config_dir() -> Path:
    """Where ModSync stores its own settings."""
    return _dir("XDG_CONFIG_HOME", ".config", "config")


def data_dir() -> Path:
    """Where ModSync stores its data (e.g. the dedicated Syncthing home)."""
    return _dir("XDG_DATA_HOME", ".local/share", "data")


def syncthing_home() -> Path:
    """Dedicated Syncthing home so we never collide with a user's own Syncthing."""
    return data_dir() / "syncthing"


def other_setup() -> Path | None:
    """Another copy's setup on this machine, which this one doesn't use: the
    Flatpak's, or the one in ``~/.config/modsync``. Two setups are two
    machines as far as pairing goes."""
    mine = config_dir()
    for candidate in (_standard_config(), flatpak_home() / "config" / APP_NAME):
        if candidate != mine and (candidate / "state.json").is_file():
            return candidate
    return None
