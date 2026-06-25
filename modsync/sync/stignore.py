"""Generate the ``.stignore`` for an MO2 instance.

The single most important line is ``/ModOrganizer.ini``: it holds every
machine-specific absolute path (game path, prefix, tool executables), so it must
never sync — each machine keeps its own. We also drop logs/caches/crash dumps and
the per-machine nxm handler registration. Everything else (mods/, profiles/,
downloads/, overwrite/) syncs as content.

Syncthing ``.stignore`` syntax: one glob per line, ``//`` for comments, a leading
``/`` anchors to the folder (instance) root.
"""

from __future__ import annotations

from pathlib import Path

PATTERNS: list[str] = [
    "// Managed by ModSync. Excludes machine-specific config and ephemera so the",
    "// same instance can sync across machines (incl. Steam Deck <-> Windows).",
    "",
    "// Holds all machine-specific absolute paths — must stay local to each machine:",
    "/ModOrganizer.ini",
    "/nxmhandler.ini",
    "",
    "// Logs, caches, crash dumps — per-machine noise:",
    "/logs",
    "/webcache",
    "/crashDumps",
    "*.log",
    "*.tmp",
    "",
    "// ModSync's own per-machine state, if any:",
    "/.modsync",
]


def stignore_text() -> str:
    return "\n".join(PATTERNS) + "\n"


def write_stignore(instance_dir: Path | str) -> Path:
    path = Path(instance_dir) / ".stignore"
    path.write_text(stignore_text(), encoding="utf-8")
    return path
