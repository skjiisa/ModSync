"""Load the downgrade recipe index and choose the archives for one downgrade.

The index (``recipes/skyrim-se.json``) is generated from Mulderland's recipe by
``scripts/build_recipe_index.py``. At runtime we fetch the copy on ModSync's
``main`` branch so users pick up new recipes without a ModSync release, fall
back to the last fetched copy, and finally to the copy bundled in the package.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from modsync import config

INDEX_URL = (
    "https://raw.githubusercontent.com/skjiisa/ModSync/main/"
    "modsync/downgrade/recipes/skyrim-se.json"
)
BUNDLED = Path(__file__).parent / "recipes" / "skyrim-se.json"
BASE_DEPOTS = ("489831", "489832", "489833")


class RecipeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Part:
    url: str
    sha1: str | None


@dataclass(frozen=True)
class Archive:
    depot: str
    language: str | None
    name: str  # e.g. "489831.7z.001"
    parts: tuple[Part, ...]

    @property
    def stem(self) -> str:  # "489831.7z"
        return self.name[:-4] if self.name.endswith(".001") else self.name


@dataclass
class Index:
    raw: dict[str, Any]
    origin: str  # "remote" | "cached" | "bundled"

    @property
    def game_appid(self) -> int:
        return int(self.raw["game"]["appid"])

    @property
    def exe_name(self) -> str:
        return str(self.raw["from"].get("exe") or self.raw["game"]["exe"])

    @property
    def from_version(self) -> str:
        return str(self.raw["from"]["version"])

    @property
    def from_exe_sha1(self) -> str:
        return str(self.raw["from"]["exe_sha1"]).lower()

    @property
    def targets(self) -> list[str]:
        return list(self.raw["targets"])

    @property
    def generated_at(self) -> str:
        return str(self.raw.get("generated_at", ""))

    @property
    def source_url(self) -> str:
        return str((self.raw.get("source") or {}).get("url", ""))

    @property
    def source_homepage(self) -> str:
        return str((self.raw.get("source") or {}).get("homepage", ""))

    @property
    def post_steps(self) -> list[str]:
        return list(self.raw.get("post_steps") or [])

    def estimated_bytes(self, target: str) -> int | None:
        kib = self.raw["targets"][target].get("estimated_kib")
        return int(kib) * 1024 if kib else None

    def deletes_for(self, target: str) -> list[str]:
        """Game-relative files the target version must not have."""
        if target not in self.raw["targets"]:
            raise RecipeError(f"no recipe from {self.from_version} to {target}")
        return [str(p) for p in self.raw["targets"][target].get("delete") or []]

    def steam_manifests(self, version: str) -> dict[str, str] | None:
        table = ((self.raw.get("steam") or {}).get("manifests") or {}).get(version)
        if not table:
            return None
        return {k: str(v) for k, v in table.items() if k.isdigit()}

    def archives_for(self, target: str, language: str) -> list[Archive]:
        """Base depots plus the depot for ``language`` (Steam code), if the
        recipe has one. English content lives in the base depots."""
        if target not in self.raw["targets"]:
            raise RecipeError(f"no recipe from {self.from_version} to {target}")
        depots: dict[str, Any] = self.raw["targets"][target]["depots"]
        out: list[Archive] = []
        for depot_id, entry in depots.items():
            lang = entry.get("language")
            if lang is not None and lang != language:
                continue
            parts = tuple(Part(p["url"], (p.get("sha1") or None)) for p in entry["parts"])
            out.append(Archive(depot_id, lang, entry["archive"], parts))
        missing = [d for d in BASE_DEPOTS if d not in {a.depot for a in out}]
        if missing:
            raise RecipeError(f"recipe for {target} lacks base depots {missing}")
        return out


def cache_path() -> Path:
    return config.data_dir() / "downgrade" / "index.json"


def _parse(text: str, origin: str) -> Index:
    data = json.loads(text)
    # Schema 1 omitted required target-specific deletions. Reject it so an
    # older remote/cache cannot override the complete bundled recipes.
    if data.get("schema") != 2 or "from" not in data or "targets" not in data:
        raise RecipeError(f"unrecognised recipe index ({origin})")
    return Index(data, origin)


def load_index(*, refresh: bool = True, timeout: float = 15.0) -> Index:
    """Remote → cached → bundled. Never raises for network problems."""
    if refresh:
        try:
            req = urllib.request.Request(INDEX_URL, headers={"User-Agent": "ModSync"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
                text = resp.read().decode("utf-8")
            idx = _parse(text, "remote")
            try:
                cp = cache_path()
                cp.parent.mkdir(parents=True, exist_ok=True)
                cp.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return idx
        except Exception:
            pass
    try:
        return _parse(cache_path().read_text(encoding="utf-8"), "cached")
    except Exception:
        pass
    return _parse(BUNDLED.read_text(encoding="utf-8"), "bundled")
