"""Locate a game's Proton prefix (compatdata). Linux-specific concept.

On Windows there is no prefix for native MO2, so ``platforms/windows.py`` will
not use this module.
"""

from __future__ import annotations

from pathlib import Path

from modsync.steam.libraries import Library


def compat_prefix(library: Library, appid: int) -> Path | None:
    pfx = library.steamapps / "compatdata" / str(appid) / "pfx"
    return pfx if pfx.exists() else None


def drive_c(library: Library, appid: int) -> Path | None:
    pfx = compat_prefix(library, appid)
    if not pfx:
        return None
    dc = pfx / "drive_c"
    return dc if dc.exists() else None
