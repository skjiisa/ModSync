#!/usr/bin/env python3
"""Regenerate ModSync's downgrade recipe index from Mulderland's NSIS recipe.

Stdlib only. Run from the repo root:

    python3 scripts/build_recipe_index.py            # fetch latest from GitHub
    python3 scripts/build_recipe_index.py --nsi path # convert a local copy

Writes ``modsync/downgrade/recipes/skyrim-se.json``. Exits 0 with "unchanged"
when nothing but the timestamp would differ, so the scheduled workflow only
commits real changes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modsync.downgrade import mulderload  # noqa: E402

RECIPES = ROOT / "modsync" / "downgrade" / "recipes"
STATIC = RECIPES / "skyrim-se.static.json"
OUT = RECIPES / "skyrim-se.json"


def _get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    headers = {"User-Agent": "ModSync-recipe-index", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:  # noqa: S310
        return r.read()


def latest_commit(repo: str, path: str, ref: str) -> str | None:
    url = f"https://api.github.com/repos/{repo}/commits?path={path}&sha={ref}&per_page=1"
    try:
        data = json.loads(_get(url))
        return data[0]["sha"] if data else None
    except Exception as exc:  # rate limit etc.; the index still works without it
        print(f"warning: could not resolve source commit: {exc}", file=sys.stderr)
        return None


def fetch_nsi(repo: str, path: str, ref: str) -> str:
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    return _get(url, accept="text/plain").decode("utf-8")


def _strip_volatile(doc: dict) -> dict:
    d = dict(doc)
    d.pop("generated_at", None)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nsi", type=Path, help="local recipe file instead of fetching")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    commit: str | None = None
    if args.nsi:
        text = args.nsi.read_text(encoding="utf-8")
    else:
        commit = latest_commit(mulderload.SOURCE_REPO, mulderload.SOURCE_PATH, mulderload.SOURCE_REF)
        text = fetch_nsi(mulderload.SOURCE_REPO, mulderload.SOURCE_PATH, commit or mulderload.SOURCE_REF)

    recipe = mulderload.parse(text)
    static = json.loads(STATIC.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    index = mulderload.build_index(recipe, static, source_commit=commit, generated_at=now)

    if args.out.exists():
        try:
            old = json.loads(args.out.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if old is not None and _strip_volatile(old) == _strip_volatile(index):
            print(f"unchanged: {args.out.relative_to(ROOT)} (source {recipe.source_version}, "
                  f"{len(recipe.targets)} targets)")
            return 0

    args.out.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out.relative_to(ROOT)}: from {recipe.source_version} -> "
          f"{', '.join(recipe.targets)} (source commit {commit or 'unknown'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
