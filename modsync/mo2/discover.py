"""Find portable MO2 instances on disk.

An instance is a directory containing ``ModOrganizer.ini`` (or ``ModOrganizer.exe``)
alongside a ``mods/`` subdirectory. ``known_roots`` are searched deeply (they're
specific install locations, possibly hidden, e.g. the Steam Tinker Launch path);
``broad_roots`` are searched shallowly with hidden/heavy dirs pruned.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

# Directory names we never descend into during a broad search.
_HEAVY = {"steamapps", "node_modules", "__pycache__", "Proton", "proton", ".cache"}


def is_instance(d: Path | str) -> bool:
    d = Path(d)
    has_marker = (d / "ModOrganizer.ini").exists() or (d / "ModOrganizer.exe").exists()
    return has_marker and (d / "mods").is_dir()


def _walk(
    root: Path,
    max_depth: int,
    prune_hidden: bool,
    found: list[Path],
    seen: set[Path],
) -> None:
    root = Path(root)
    if not root.exists():
        return
    root_parts = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        d = Path(dirpath)
        if (("ModOrganizer.ini" in filenames) or ("ModOrganizer.exe" in filenames)) and (
            d / "mods"
        ).is_dir():
            rp = d.resolve()
            if rp not in seen:
                seen.add(rp)
                found.append(rp)
            dirnames[:] = []  # don't descend into a found instance
            continue
        if len(d.parts) - root_parts >= max_depth:
            dirnames[:] = []
            continue
        kept = []
        for name in dirnames:
            if name in _HEAVY:
                continue
            if prune_hidden and name.startswith("."):
                continue
            kept.append(name)
        dirnames[:] = kept


def discover_instances(
    broad_roots: Iterable[Path],
    known_roots: Iterable[Path],
    broad_depth: int = 4,
    known_depth: int = 8,
) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for kr in known_roots:
        _walk(Path(kr), known_depth, prune_hidden=False, found=found, seen=seen)
    for br in broad_roots:
        _walk(Path(br), broad_depth, prune_hidden=True, found=found, seen=seen)
    return found
