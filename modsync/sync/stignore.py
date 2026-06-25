"""Generate the ``.stignore`` for an MO2 instance.

ModSync syncs **only MO2 content** — the mod list, load order, profiles, and (if
enabled) downloads. Everything else at the instance root is excluded, because a
guided-installed instance dir also contains:

* the MO2 program binaries (ModOrganizer.exe, usvfs/Qt DLLs, ...) — machine-local,
  installed separately on each machine;
* ``ModOrganizer.ini`` — holds machine-specific absolute paths (game, prefix, tools);
* ``nexusApiKey`` — a per-user secret that must never sync;
* logs, caches, and crash dumps.

Syncthing ``.stignore``: one glob per line, ``//`` for comments, a leading ``/``
anchors to the folder (instance) root, and first match wins. We negate the content
dirs, then ignore everything else with ``/*`` (ignoring a top-level dir ignores its
whole subtree, so the program/cache folders are skipped entirely).
"""

from __future__ import annotations

from pathlib import Path

# The MO2 content directories we sync (default portable-instance layout).
CONTENT_DIRS = ("mods", "profiles", "downloads", "overwrite")

PATTERNS: list[str] = [
    "// Managed by ModSync. Syncs ONLY Mod Organizer 2 content — never the MO2",
    "// program binaries, the machine-specific ModOrganizer.ini, or nexusApiKey.",
    "",
    "// Content to keep in sync:",
    *(f"!/{name}" for name in CONTENT_DIRS),
    "!/categories.dat",
    "",
    "// Exclude everything else at the instance root (program files, ModOrganizer.ini,",
    "// nexusApiKey, logs, webcache, crashDumps, ...). Ignoring a dir skips its subtree.",
    "/*",
]


def stignore_text() -> str:
    return "\n".join(PATTERNS) + "\n"


def write_stignore(instance_dir: Path | str) -> Path:
    path = Path(instance_dir) / ".stignore"
    path.write_text(stignore_text(), encoding="utf-8")
    return path
