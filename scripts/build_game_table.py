#!/usr/bin/env python3
"""Regenerate ModSync's table of Mod Organizer 2 games from MO2-LINT's.

MO2-LINT keeps what it knows about each game in ``configs/game_info.yml``: its
ids in Steam, GOG and Epic, the name MO2 gives it, Nexus' name for it, the
program each store starts and the winetricks its prefix needs. ModSync reads
the same facts from ``modsync/mo2/games.json``, which this writes, so a game
MO2-LINT can install is one ModSync can run without anyone adding it by hand.

Run from the repo root after changing the MO2-LINT pin
(``modsync/mo2/installers/mo2lint.py``):

    uv run --with pyyaml python scripts/build_game_table.py          # the pinned release, from GitHub
    uv run --with pyyaml python scripts/build_game_table.py --yml f  # a local game_info.yml

A game's ``parent`` is folded in the way MO2-LINT does it: what the child sets
wins, and tables (the ids, the programs) merge key by key.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modsync.mo2.installers.mo2lint import MO2LINT_VERSION  # noqa: E402

OUT = ROOT / "modsync" / "mo2" / "games.json"
SOURCE = ("https://raw.githubusercontent.com/Furglitch/modorganizer2-linux-installer/"
          f"{MO2LINT_VERSION}/configs/game_info.yml")
STORES = ("steam", "gog", "epic")


def _per_store(value) -> dict:
    """A value MO2-LINT writes once for every store, or once per store."""
    if isinstance(value, dict):
        return {store: value.get(store) for store in STORES}
    return {store: value for store in STORES}


def _merged(games: dict, key: str) -> dict:
    """The game's entry with its parent's folded in."""
    entry = dict(games[key])
    parent = entry.pop("parent", None)
    if not parent:
        return entry
    base = _merged(games, parent)
    for name, value in entry.items():
        if value in (None, [], {}, ()):
            continue
        if name in ("executable", "subdirectory") and (isinstance(value, dict) or isinstance(base.get(name), dict)):
            # One side may say it once and the other per store.
            sides = _per_store(base.get(name)), (_per_store(value) if not isinstance(value, dict) else value)
            base[name] = {**sides[0], **{k: v for k, v in sides[1].items() if v is not None}}
        elif isinstance(value, dict) and isinstance(base.get(name), dict):
            base[name] = {**base[name], **value}
        else:
            base[name] = value
    return base


def convert(yml: dict) -> dict:
    games = yml.get("games") or {}
    out: dict[str, dict] = {}
    for key in games:
        entry = _merged(games, key)
        ids = entry.get("launcher_ids") or {}
        programs = _per_store(entry.get("executable"))
        game = {
            "name": entry["display_name"],  # also what MO2-LINT writes as MO2's gameName
            "nexus": entry["nexus_slug"],
            "steam": int(ids["steam"]) if ids.get("steam") else None,
            "gog": str(ids["gog"]) if ids.get("gog") else None,
            "epic": str(ids["epic"]) if ids.get("epic") else None,
            "exe": {store: programs[store] for store in STORES if ids.get(store) and programs.get(store)},
            "tricks": list(entry.get("tricks") or []),
        }
        out[key] = game
    return {"mo2lint": MO2LINT_VERSION, "games": out}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yml", type=Path, help="a local game_info.yml instead of the pinned release's")
    args = parser.parse_args()
    try:
        import yaml
    except ImportError:
        print("PyYAML is needed: uv run --with pyyaml python scripts/build_game_table.py", file=sys.stderr)
        return 2
    if args.yml:
        text = args.yml.read_text(encoding="utf-8")
    else:
        request = urllib.request.Request(SOURCE, headers={"User-Agent": "ModSync-game-table"})
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (a fixed GitHub URL)
            text = response.read().decode("utf-8")
    table = convert(yaml.safe_load(text))
    rendered = json.dumps(table, indent=2, ensure_ascii=False) + "\n"
    if OUT.exists() and OUT.read_text(encoding="utf-8") == rendered:
        print(f"{OUT.relative_to(ROOT)} unchanged ({len(table['games'])} games, MO2-LINT {MO2LINT_VERSION})")
        return 0
    OUT.write_text(rendered, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(table['games'])} games from MO2-LINT {MO2LINT_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
