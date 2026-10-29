#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version (release-notes helper).

    scripts/changelog_section.py [--collapse] 0.1.0-rc1

Matches the `## [<version>]` heading and prints everything up to the next
`## ` heading. Exits 1 when the version has no section, so the release
workflow can fall back to a generic line.

With --collapse, the section's intro (everything before its first `### `
heading) stays visible and the rest goes in a collapsed `<details>` block, so
a release page leads with the summary.
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


def collapse(body: str) -> str:
    m = re.search(r"^### ", body, re.M)
    if m is None:
        return body
    intro, details = body[:m.start()].strip(), body[m.start():].strip()
    # GitHub only renders Markdown inside <details> with blank lines around it.
    folded = f"<details>\n<summary>Full changelog</summary>\n\n{details}\n\n</details>"
    return f"{intro}\n\n{folded}" if intro else folded


def main(argv: list[str]) -> int:
    folded = "--collapse" in argv
    args = [a for a in argv if a != "--collapse"]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    body = section(path.read_text(encoding="utf-8"), args[0])
    if body is None:
        print(f"no CHANGELOG section for {args[0]}", file=sys.stderr)
        return 1
    print(collapse(body) if folded else body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
