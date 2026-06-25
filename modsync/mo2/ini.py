"""Targeted reader + helpers for ModOrganizer.ini.

For Phase 1 we only *read* the ini (to discover layout). Values are returned
verbatim; the helpers interpret the MO2/Qt encodings:

* ``@ByteArray(...)`` wrapping with doubled backslashes  (gamePath, gameName, ...)
* Wine/Proton ``Z:\\...`` paths  (``Z:`` maps to ``/``)
* path tokens like ``%BASE_DIR%``

When we later need to *write* the ini, we'll use line-targeted edits to preserve
``@ByteArray(...)``/``@Variant(...)`` blobs byte-for-byte (see plan).
"""

from __future__ import annotations

import re
from pathlib import Path

_BYTEARRAY_RE = re.compile(r"^@ByteArray\((.*)\)$", re.DOTALL)
_DRIVE_RE = re.compile(r"^([A-Za-z]):/*(.*)$", re.DOTALL)
_TOKEN_RE = re.compile(r"%([A-Z_]+)%")


def read_ini(path: Path | str) -> dict[str, dict[str, str]]:
    """Read ModOrganizer.ini into ``{section: {key: raw_value}}``.

    Keys are kept as written (MO2/Qt usually lower-cases them, e.g. ``gamepath``,
    ``download_directory``). Use :func:`get_ci` for case-insensitive lookups.
    """
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return sections
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in ";#":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = sections.setdefault(stripped[1:-1], {})
            continue
        if current is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        current[key.strip()] = value.strip()
    return sections


def get_ci(section: dict[str, str], *keys: str) -> str | None:
    """Case-insensitive lookup across candidate key spellings."""
    lowered = {k.lower(): v for k, v in section.items()}
    for k in keys:
        if k.lower() in lowered:
            return lowered[k.lower()]
    return None


def unwrap_bytearray(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    m = _BYTEARRAY_RE.match(value)
    if m:
        value = m.group(1)
    # ModOrganizer.ini doubles backslashes inside @ByteArray; collapse them.
    return value.replace("\\\\", "\\")


def wine_to_local(path: str | None) -> Path | None:
    """Translate a Wine/Proton path (e.g. ``Z:\\home\\deck\\x``) to a host path.

    ``Z:`` maps to ``/``. Other drive letters map into a Proton prefix or another
    mount we can't resolve here, so return ``None`` rather than guess. A path that
    is already POSIX (starts with ``/``) is returned as-is.
    """
    if not path:
        return None
    text = path.strip().replace("\\", "/")
    m = _DRIVE_RE.match(text)
    if m:
        drive, rest = m.group(1), m.group(2)
        if drive.lower() == "z":
            return Path("/" + rest.lstrip("/"))
        return None
    if text.startswith("/"):
        return Path(text)
    return None


def expand_tokens(value: str, base_dir: str | None) -> str:
    """Expand MO2 path tokens such as ``%BASE_DIR%``. Unknown tokens stay intact."""

    def repl(m: re.Match[str]) -> str:
        if m.group(1) == "BASE_DIR" and base_dir:
            return base_dir
        return m.group(0)

    return _TOKEN_RE.sub(repl, value)
