"""Steam Play compatibility tools: which exist, where they live, what they need.

Steam knows two kinds. **Valve's own** (Proton Experimental, Proton 9.0, …)
are Steam apps: ``appinfo.vdf`` app 891390 ("Steam Play 2.0 Manifests") maps
the tool names ``config.vdf`` uses (``proton_experimental``) to appids, and each
is installed under ``steamapps/common`` like a game. **Custom** ones (GE-Proton,
Steam Tinker Launch, MO2-LINT's redirector, ModSync's launch hook) live in
``<steam root>/compatibilitytools.d/<dir>/`` and describe themselves in a
``compatibilitytool.vdf``.

On ARM64 (the Steam Frame) Valve's ARM builds are declared in a second
manifests app, 3043620 ("Steam Frame ARM64 Compat List"), under names ending in
``-arm64`` (``proton-experimental-arm64``, ``proton_11-arm64``). Steam on ARM64
resolves a selected tool to its ``-arm64`` counterpart first and only falls
back to the name as given, so ``proton_experimental`` in ``config.vdf`` runs
Proton Experimental (ARM64). :func:`find_tool` does the same.

Either kind carries a ``toolmanifest.vdf`` whose ``require_tool_appid`` names
the Steam Linux Runtime container the tool must run inside (soldier 1391110,
sniper 1628350, 4.0 4183110, 4.0 arm64 4185400). Steam sets that container up
around a tool; a tool that wants to run *outside* it — to show a window on the
host before the game starts — has to set it up itself, which is what the launch
hook does.
"""

from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from modsync.steam import appinfo, vdf
from modsync.steam.libraries import Library, find_app

STEAM_PLAY_MANIFESTS_APPID = 891390
ARM64_MANIFESTS_APPID = 3043620
# The main manifest comes first so its names win when both declare one.
MANIFEST_APPIDS = (STEAM_PLAY_MANIFESTS_APPID, ARM64_MANIFESTS_APPID)
ARM64_SUFFIX = "-arm64"
# What "Default" means in a game's Compatibility menu (the manifests'
# ``selectable_aliases``): Proton 11.0 on x86_64, Proton 11.0 (ARM64) on ARM64.
STEAM_DEFAULT_TOOL = "proton-stable"
# Valve's per-device compatibility profile in a game's appinfo, by SteamOS
# VARIANT_ID; its ``recommended_runtime`` beats the global default.
DEVICE_PROFILE_KEYS = {"steamdeck": "steam_deck_compatibility", "vr": "steam_frame_compatibility"}


def is_arm64() -> bool:
    return platform.machine().lower() in ("aarch64", "arm64")

# MO2-LINT (PR #1096 onwards) installs one of these per game; selecting it in
# Steam makes Play start Mod Organizer 2 instead of the game.
MO2LINT_TOOL_RE = re.compile(r"^mo2_(\d+)_redirector$")


@dataclass(frozen=True)
class CompatTool:
    name: str  # the id CompatToolMapping uses, e.g. "GE-Proton10-34", "proton_experimental"
    display_name: str
    path: Path
    kind: str  # "valve" | "custom"
    aliases: tuple[str, ...] = ()

    @property
    def is_mo2lint(self) -> bool:
        return MO2LINT_TOOL_RE.match(self.name) is not None

    @property
    def runnable(self) -> bool:
        return (self.path / "proton").exists() or (self.path / "toolmanifest.vdf").exists()


def compatibilitytools_dir(steam_root: Path | str) -> Path:
    return Path(steam_root) / "compatibilitytools.d"


def _ci(d: dict, key: str) -> object:
    kl = key.lower()
    for k, v in d.items():
        if k.lower() == kl:
            return v
    return None


def _tools_from_manifest(manifest: Path, base: Path) -> list[CompatTool]:
    try:
        data = vdf.load(manifest)
    except OSError:
        return []
    block = _ci(data, "compatibilitytools")
    tools = _ci(block, "compat_tools") if isinstance(block, dict) else None
    if not isinstance(tools, dict):
        return []
    out: list[CompatTool] = []
    for name, spec in tools.items():
        if not isinstance(spec, dict):
            continue
        install = str(_ci(spec, "install_path") or ".")
        path = Path(install) if Path(install).is_absolute() else (base / install)
        out.append(
            CompatTool(
                name=str(name),
                display_name=str(_ci(spec, "display_name") or name),
                path=path.resolve() if path.exists() else path,
                kind="custom",
            )
        )
    return out


def custom_tools(steam_root: Path | str) -> list[CompatTool]:
    """Everything registered under ``compatibilitytools.d``."""
    root = compatibilitytools_dir(steam_root)
    if not root.is_dir():
        return []
    out: list[CompatTool] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            manifest = entry / "compatibilitytool.vdf"
            if manifest.is_file():
                out.extend(_tools_from_manifest(manifest, entry))
        elif entry.suffix == ".vdf":
            out.extend(_tools_from_manifest(entry, root))
    return out


def _manifest_entries(steam_root: Path | str) -> list[tuple[str, dict]]:
    """Every Windows compatibility tool the Steam Play manifests declare,
    installed or not, as ``(name, spec)``; the main manifest first."""
    path = appinfo.appinfo_path(steam_root)
    if not path.exists():
        return []
    try:
        infos = appinfo.read_apps(path, MANIFEST_APPIDS)
    except (OSError, appinfo.AppInfoError):
        return []
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for manifest_appid in MANIFEST_APPIDS:
        info = infos.get(manifest_appid)
        if info is None:
            continue
        ext = info.raw.get("extended") or {}
        for name, spec in (ext.get("compat_tools") or {}).items():
            if name in seen or not isinstance(spec, dict) or str(spec.get("from_oslist", "")) != "windows":
                continue
            seen.add(name)
            out.append((str(name), spec))
    return out


def _aliases(spec: dict) -> tuple[str, ...]:
    return tuple(a for a in str(spec.get("aliases") or "").split(",") if a)


def valve_tools(steam_root: Path | str, libraries: Iterable[Library]) -> list[CompatTool]:
    """Valve's Proton builds that are installed, named as ``config.vdf`` names them."""
    libraries = list(libraries)
    out: list[CompatTool] = []
    for name, spec in _manifest_entries(steam_root):
        try:
            tool_appid = int(spec.get("appid"))
        except (TypeError, ValueError):
            continue
        app = find_app(libraries, tool_appid)
        if app is None or not app.install_path.is_dir():
            continue
        out.append(
            CompatTool(
                name=name,
                display_name=str(spec.get("display_name") or name),
                path=app.install_path,
                kind="valve",
                aliases=_aliases(spec),
            )
        )
    return out


def all_tools(steam_root: Path | str, libraries: Iterable[Library]) -> list[CompatTool]:
    return custom_tools(steam_root) + valve_tools(steam_root, list(libraries))


def arm64_candidates(name: str, steam_root: Path | str) -> list[str]:
    """The names Steam on ARM64 tries for a selected tool, in order: the
    ``-arm64`` name, the ARM64 tool that shares an alias with it (the ARM64
    Proton Experimental is ``proton-experimental-arm64``, not
    ``proton_experimental-arm64``), then the name itself."""
    candidates = [] if name.endswith(ARM64_SUFFIX) else [name + ARM64_SUFFIX]
    entries = _manifest_entries(steam_root)
    names = {name}
    for entry_name, spec in entries:
        if name == entry_name or name in _aliases(spec):
            names = {entry_name, *_aliases(spec)}
            break
    for entry_name, spec in entries:
        if entry_name.endswith(ARM64_SUFFIX) and names & {entry_name, *_aliases(spec)}:
            candidates.append(entry_name)
            break
    candidates.append(name)
    return list(dict.fromkeys(candidates))


def find_tool(name: str, steam_root: Path | str, libraries: Iterable[Library]) -> CompatTool | None:
    """The installed tool Steam runs for ``name`` (a ``config.vdf`` tool name
    or an alias such as ``proton-stable``)."""
    tools = all_tools(steam_root, libraries)
    for candidate in arm64_candidates(name, steam_root) if is_arm64() else [name]:
        for tool in tools:
            if tool.name == candidate:
                return tool
        for tool in tools:
            if candidate in tool.aliases:
                return tool
    return None


def _steamos_variant() -> str | None:
    try:
        lines = Path("/etc/os-release").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    fields = dict(line.split("=", 1) for line in lines if "=" in line)
    fields = {k: v.strip().strip("\"'") for k, v in fields.items()}
    return fields.get("VARIANT_ID") if fields.get("ID") == "steamos" else None


def recommended_tools(appid: int, steam_root: Path | str) -> list[str]:
    """What Valve recommends for the game before Steam falls back to the
    global default: the device's compatibility profile on a Steam Deck or
    Steam Frame, then the per-game mapping in the Steam Play manifests."""
    path = appinfo.appinfo_path(steam_root)
    if not path.exists():
        return []
    try:
        infos = appinfo.read_apps(path, (appid, *MANIFEST_APPIDS))
    except (OSError, appinfo.AppInfoError):
        return []
    names: list[str] = []
    key = DEVICE_PROFILE_KEYS.get(_steamos_variant() or "")
    game = infos.get(appid)
    if key and game is not None:
        profile = (game.raw.get("common") or {}).get(key) or {}
        runtime = (profile.get("configuration") or {}).get("recommended_runtime") if isinstance(profile, dict) else None
        if runtime and runtime != "native":
            names.append(str(runtime))
    for manifest_appid in MANIFEST_APPIDS:
        info = infos.get(manifest_appid)
        mapping = ((info.raw.get("extended") or {}).get("app_mappings") or {}).get(str(appid)) if info else None
        if isinstance(mapping, dict) and mapping.get("tool"):
            names.append(str(mapping["tool"]))
    return names


def steam_default_tool(
    appid: int, steam_root: Path | str, libraries: Iterable[Library], global_choice: str | None = None
) -> CompatTool | None:
    """The tool Steam runs for a game left on "Default", in Steam's order:
    Valve's recommendation for the game, the user's global choice
    (``CompatToolMapping`` entry 0), then Proton stable."""
    libraries = list(libraries)
    for name in [*recommended_tools(appid, steam_root), global_choice, STEAM_DEFAULT_TOOL]:
        if name and (tool := find_tool(name, steam_root, libraries)) is not None:
            return tool
    return default_valve_tool(steam_root, libraries)


def mo2lint_tool(appid: int, steam_root: Path | str) -> CompatTool | None:
    """MO2-LINT's per-game redirector tool, if that version of MO2-LINT installed one."""
    for tool in custom_tools(steam_root):
        if tool.name == f"mo2_{appid}_redirector":
            return tool
    return None


def default_valve_tool(steam_root: Path | str, libraries: Iterable[Library]) -> CompatTool | None:
    """A last resort when :func:`steam_default_tool` finds nothing Steam would
    pick: Proton Experimental, else the newest numbered Proton, else Proton
    Hotfix. On ARM64 the ARM64 builds of those come first."""
    tools = {t.name: t for t in valve_tools(steam_root, libraries)}
    if is_arm64():
        # proton-experimental-arm64 -> proton_experimental, proton_11-arm64 -> proton_11
        arm = {
            n.removesuffix(ARM64_SUFFIX).replace("-", "_"): t for n, t in tools.items() if n.endswith(ARM64_SUFFIX)
        }
        tools = arm or tools
    if "proton_experimental" in tools:
        return tools["proton_experimental"]
    numbered = sorted(
        (int(m.group(1)), t) for name, t in tools.items() if (m := re.fullmatch(r"proton_(\d+)", name))
    )
    if numbered:
        return numbered[-1][1]
    return tools.get("proton_hotfix")


def require_tool_appid(tool_path: Path | str) -> int | None:
    """The Steam Linux Runtime app the tool's ``toolmanifest.vdf`` asks for."""
    manifest = Path(tool_path) / "toolmanifest.vdf"
    try:
        data = vdf.load(manifest)
    except OSError:
        return None
    block = _ci(data, "manifest")
    if not isinstance(block, dict):
        return None
    try:
        return int(str(_ci(block, "require_tool_appid") or ""))
    except ValueError:
        return None


def runtime_path(runtime_appid: int, libraries: Iterable[Library]) -> Path | None:
    """Where a Steam Linux Runtime app is installed (its ``_v2-entry-point`` dir)."""
    app = find_app(list(libraries), runtime_appid)
    if app is None:
        return None
    return app.install_path if (app.install_path / "_v2-entry-point").exists() else None
