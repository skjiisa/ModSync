"""Steam Play compatibility tools: which exist, where they live, what they need.

Steam knows two kinds. **Valve's own** (Proton Experimental, Proton 9.0, …)
are Steam apps: ``appinfo.vdf`` app 891390 ("Steam Play 2.0 Manifests") maps
the tool names ``config.vdf`` uses (``proton_experimental``) to appids, and each
is installed under ``steamapps/common`` like a game. **Custom** ones (GE-Proton,
Steam Tinker Launch, MO2-LINT's redirector, ModSync's launch hook) live in
``<steam root>/compatibilitytools.d/<dir>/`` and describe themselves in a
``compatibilitytool.vdf``.

Either kind carries a ``toolmanifest.vdf`` whose ``require_tool_appid`` names
the Steam Linux Runtime container the tool must run inside (soldier 1391110,
sniper 1628350, 4.0 4183110). Steam sets that container up around a tool; a
tool that wants to run *outside* it — to show a window on the host before the
game starts — has to set it up itself, which is what the launch hook does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from modsync.steam import appinfo, vdf
from modsync.steam.libraries import Library, find_app

STEAM_PLAY_MANIFESTS_APPID = 891390

# MO2-LINT (PR #1096 onwards) installs one of these per game; selecting it in
# Steam makes Play start Mod Organizer 2 instead of the game.
MO2LINT_TOOL_RE = re.compile(r"^mo2_(\d+)_redirector$")


@dataclass(frozen=True)
class CompatTool:
    name: str  # the id CompatToolMapping uses, e.g. "GE-Proton10-34", "proton_experimental"
    display_name: str
    path: Path
    kind: str  # "valve" | "custom"

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


def valve_tools(steam_root: Path | str, libraries: Iterable[Library]) -> list[CompatTool]:
    """Valve's Proton builds that are installed, named as ``config.vdf`` names them."""
    path = appinfo.appinfo_path(steam_root)
    if not path.exists():
        return []
    try:
        info = appinfo.read_app(path, STEAM_PLAY_MANIFESTS_APPID)
    except (OSError, appinfo.AppInfoError):
        return []
    if info is None:
        return []
    ext = info.raw.get("extended") or {}
    specs = ext.get("compat_tools") or {}
    libraries = list(libraries)
    out: list[CompatTool] = []
    for name, spec in specs.items():
        if not isinstance(spec, dict) or str(spec.get("from_oslist", "")) != "windows":
            continue
        try:
            tool_appid = int(spec.get("appid"))
        except (TypeError, ValueError):
            continue
        app = find_app(libraries, tool_appid)
        if app is None or not app.install_path.is_dir():
            continue
        out.append(
            CompatTool(
                name=str(name),
                display_name=str(spec.get("display_name") or name),
                path=app.install_path,
                kind="valve",
            )
        )
    return out


def all_tools(steam_root: Path | str, libraries: Iterable[Library]) -> list[CompatTool]:
    return custom_tools(steam_root) + valve_tools(steam_root, list(libraries))


def find_tool(name: str, steam_root: Path | str, libraries: Iterable[Library]) -> CompatTool | None:
    for tool in all_tools(steam_root, libraries):
        if tool.name == name:
            return tool
    return None


def mo2lint_tool(appid: int, steam_root: Path | str) -> CompatTool | None:
    """MO2-LINT's per-game redirector tool, if that version of MO2-LINT installed one."""
    for tool in custom_tools(steam_root):
        if tool.name == f"mo2_{appid}_redirector":
            return tool
    return None


def default_valve_tool(steam_root: Path | str, libraries: Iterable[Library]) -> CompatTool | None:
    """The Proton Steam most likely picks for a game with no explicit choice:
    Proton Experimental, else the newest numbered Proton, else Proton Hotfix."""
    tools = {t.name: t for t in valve_tools(steam_root, libraries)}
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
