"""Build a human-readable "what's on this machine" report, shared by the
`doctor` CLI and the GUI dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO

from modsync import platforms
from modsync.games import SKYRIM_SE
from modsync.mo2 import discover as mo2_discover
from modsync.mo2 import instance as mo2_instance
from modsync.steam import libraries as libs
from modsync.steam import prefixes


@dataclass
class Report:
    text: str
    steam_found: bool


def build() -> Report:
    out = StringIO()

    def line(s: str = "") -> None:
        out.write(s + "\n")

    plat = platforms.current()
    line(f"Platform: {plat.name}")
    line()

    roots = plat.steam_roots()
    if not roots:
        line("✗ No Steam installation found.")
        return Report(out.getvalue(), False)

    line("Steam roots:")
    for r in roots:
        line(f"  • {r}")
    line()

    libraries = libs.all_libraries(roots)
    line(f"Libraries ({len(libraries)}):")
    for lib in libraries:
        line(f"  • {lib.path}  ({len(lib.app_ids)} apps indexed)")
    line()

    app = libs.find_app(libraries, SKYRIM_SE.appid)
    line(f"{SKYRIM_SE.name} ({SKYRIM_SE.appid}):")
    if app:
        line(f"  ✓ installed: {app.install_path}")
        pfx = prefixes.compat_prefix(app.library, SKYRIM_SE.appid)
        if pfx:
            line(f"  • Proton prefix: {pfx}")
        else:
            line("  • Proton prefix: (none yet — game has not been run under Proton)")
    else:
        line("  ✗ not installed")
    line()

    line("Mod Organizer 2 instances:")
    instances = mo2_discover.discover_instances(
        plat.mo2_broad_roots(), plat.mo2_known_roots()
    )
    if not instances:
        line("  (none found — run the guided setup to create one)")
    for path in instances:
        info = mo2_instance.inspect(path)
        line(f"  • {path}")
        line(f"      game: {info.game_name or '?'}    profile: {info.selected_profile or '?'}")
        if info.game_path_local:
            line(f"      gamePath → {info.game_path_local}")
        for name, cd in info.content_dirs.items():
            tag = "inside " if cd.inside_instance else "OUTSIDE"
            miss = "" if cd.exists else "  (missing)"
            line(f"      {name:<9} [{tag}] {cd.path}{miss}")
        if info.profiles:
            line(f"      profiles: {', '.join(info.profiles)}")
        for issue in info.issues:
            line(f"      ! {issue}")

    return Report(out.getvalue(), True)
