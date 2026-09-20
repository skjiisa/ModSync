#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version (release-notes helper).

    scripts/changelog_section.py 0.1.0-rc1

Matches the `## [<version>]` heading and prints everything up to the next
`## ` heading. Exits 1 when the version has no section, so the release
workflow can fall back to a generic line.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def section(text: str, version: str) -> str | None:
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    body = section(path.read_text(encoding="utf-8"), argv[0])
    if body is None:
        print(f"no CHANGELOG section for {argv[0]}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
