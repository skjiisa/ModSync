"""Inspect a portable MO2 instance: where its content dirs are, whether they're
covered by sync (i.e. inside the instance), the selected game/profile, and any
issues worth surfacing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from modsync.mo2 import ini as ini_mod

# MO2 [Settings] keys for each content dir, and the default subdir name when unset.
CONTENT_DIRS: dict[str, str] = {
    "mods": "mod_directory",
    "downloads": "download_directory",
    "profiles": "profiles_directory",
    "overwrite": "overwrite_directory",
}


@dataclass
class ContentDir:
    name: str
    raw: str | None
    path: Path | None
    inside_instance: bool
    exists: bool


@dataclass
class Mo2Instance:
    path: Path
    ini_path: Path
    has_ini: bool
    game_name: str
    game_path_raw: str | None
    game_path_local: Path | None
    selected_profile: str
    content_dirs: dict[str, ContentDir]
    profiles: list[str]
    issues: list[str] = field(default_factory=list)


def _resolve_dir(
    raw: str | None, instance: Path, base_dir: Path, default_subdir: str
) -> Path | None:
    if not raw:
        return instance / default_subdir
    expanded = ini_mod.expand_tokens(raw, str(base_dir))
    local = ini_mod.wine_to_local(expanded)
    if local is not None:
        return local
    p = Path(expanded)
    if not p.is_absolute():
        return (instance / expanded).resolve()
    return p


def _is_inside(path: Path | None, instance: Path) -> bool:
    if path is None:
        return False
    try:
        return path == instance or path.is_relative_to(instance)
    except (ValueError, OSError):
        return False


def inspect(path: Path | str) -> Mo2Instance:
    instance = Path(path).resolve()
    ini_path = instance / "ModOrganizer.ini"
    sections = ini_mod.read_ini(ini_path)
    has_ini = ini_path.exists() and bool(sections)
    general = sections.get("General", {})
    settings = sections.get("Settings", {})

    game_name = ini_mod.unwrap_bytearray(ini_mod.get_ci(general, "gameName")) or ""
    gp_unwrapped = ini_mod.unwrap_bytearray(ini_mod.get_ci(general, "gamePath"))
    gp_local = ini_mod.wine_to_local(gp_unwrapped) if gp_unwrapped else None
    selected = ini_mod.unwrap_bytearray(ini_mod.get_ci(general, "selected_profile")) or ""

    base_dir = instance
    base_raw = ini_mod.get_ci(settings, "base_directory")
    if base_raw:
        resolved_base = ini_mod.wine_to_local(ini_mod.expand_tokens(base_raw, str(instance)))
        if resolved_base is not None:
            base_dir = resolved_base

    content: dict[str, ContentDir] = {}
    issues: list[str] = []
    for name, key in CONTENT_DIRS.items():
        raw = ini_mod.get_ci(settings, key)
        resolved = _resolve_dir(raw, instance, base_dir, name)
        inside = _is_inside(resolved, instance)
        exists = bool(resolved and resolved.exists())
        content[name] = ContentDir(name, raw, resolved, inside, exists)
        if resolved is not None and not inside:
            issues.append(
                f"'{name}' resolves OUTSIDE the instance ({resolved}); "
                "it would not be covered by sync."
            )

    profiles_dir = content["profiles"].path or (instance / "profiles")
    profiles: list[str] = []
    if profiles_dir.is_dir():
        profiles = sorted(p.name for p in profiles_dir.iterdir() if p.is_dir())

    if not has_ini:
        issues.append("No readable ModOrganizer.ini found.")

    return Mo2Instance(
        path=instance,
        ini_path=ini_path,
        has_ini=has_ini,
        game_name=game_name,
        game_path_raw=gp_unwrapped,
        game_path_local=gp_local,
        selected_profile=selected,
        content_dirs=content,
        profiles=profiles,
        issues=issues,
    )
