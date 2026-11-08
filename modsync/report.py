"""Build a human-readable "what's on this machine" report, shared by the
`doctor` CLI and the GUI dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from modsync import gameversion, launchhook, platforms
from modsync.games import CYBERPUNK, SKYRIM_SE
from modsync.heroic import library as heroic
from modsync.mo2 import discover as mo2_discover
from modsync.mo2 import instance as mo2_instance
from modsync.mo2 import launch as mo2_launch
from modsync.mo2 import usvfs as usvfs_fix, vcruntime
from modsync.steam import pe
from modsync.steam import libraries as libs
from modsync.state import State
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
        line()
        _heroic_section(plat, line)
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
        version = gameversion.installed_version(app.install_path)
        line(f"  • runtime version: {version or '(not detected)'}")
        pfx = prefixes.compat_prefix(app.library, SKYRIM_SE.appid)
        if pfx:
            line(f"  • Proton prefix: {pfx}")
        else:
            line("  • Proton prefix: (none yet — game has not been run under Proton)")
    else:
        line("  ✗ not installed")
    line()

    _heroic_section(plat, line)

    usvfs_messages: dict[Path, str] = {}

    def usvfs_message(instance: Path) -> str:
        key = instance.resolve()
        if key not in usvfs_messages:
            usvfs_messages[key] = usvfs_fix.status(key).message
        return usvfs_messages[key]

    line("Mod Organizer 2 instances:")
    instances = list(
        mo2_discover.discover_instances(plat.mo2_broad_roots(), plat.mo2_known_roots())
    )
    seen = {p.resolve() for p in instances}
    for p in mo2_discover.mo2lint_instances():  # known via MO2-LINT, even pre-launch
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp not in seen and rp.exists():
            seen.add(rp)
            instances.append(rp)
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
        usvfs = pe.file_version(path / "usvfs_x64.dll")
        if usvfs:
            shown = ".".join(map(str, usvfs))
            if vcruntime.usvfs_needs_prefix_runtime(path):
                try:
                    problems = vcruntime.prefix_runtime_problems(mo2_launch.prefix_system32(path))
                except (RuntimeError, OSError):
                    problems = None
                if problems is None:
                    line(f"      usvfs {shown}: needs a current VC++ runtime in the game prefix (not checked)")
                elif problems:
                    line(f"      ! usvfs {shown} needs a current VC++ runtime in the game prefix: {'; '.join(problems)}")
                else:
                    line(f"      usvfs {shown}: game prefix has a current VC++ runtime")
            else:
                line(f"      usvfs {shown}")
        for issue in info.issues:
            line(f"      ! {issue}")
        if usvfs_fix.worth_checking(path):
            line(f"      USVFS ARM64: {usvfs_message(path)}")

    hook = launchhook.status()
    line()
    line(f"Steam launch hook ({SKYRIM_SE.name}): {'on' if hook.enabled else 'off'}")
    line(f"  • {hook.summary()}")

    state = State.load()
    if state.has_instance:
        line()
        line("ModSync setup:")
        line(f"  • instance: {state.instance_path}")
        if usvfs_fix.worth_checking(state.instance_path):
            line(f"  • USVFS ARM64: {usvfs_message(Path(state.instance_path))}")
        line(f"  • sync: {'vault ' + str(state.folder_id) if state.syncing else 'off'}")
        vc = gameversion.check(state.instance_path)
        mark = "!" if vc.mismatch else "•"
        line(f"  {mark} {vc.summary()}")
        if vc.skse_note():
            line(f"  {'!' if vc.skse_suggests else '•'} {vc.skse_note()}")

    return Report(out.getvalue(), True)


def _heroic_section(plat: platforms.Platform, line) -> None:
    """Heroic and the GOG games ModSync supports, when Heroic is installed."""
    config_dirs = plat.heroic_config_dirs()
    if not config_dirs:
        return
    line("Heroic config:")
    for d in config_dirs:
        line(f"  • {d}")
    line()
    game = heroic.find_gog_game(CYBERPUNK.gog_id, config_dirs)
    line(f"{CYBERPUNK.name} (GOG {CYBERPUNK.gog_id}, Heroic):")
    if game is None:
        line("  ✗ not installed")
        line()
        return
    line(f"  ✓ installed: {game.install_path}")
    build = game.version or "(unknown)"
    line(f"  • GOG build: {build}{' (pinned in Heroic)' if game.pinned else ''}")
    if game.prefix is None:
        line("  • prefix: (none yet — game has not been run from Heroic)")
    else:
        line(f"  • prefix: {game.prefix}")
    if game.wine is not None:
        line(f"  • runs with: {game.wine.name or game.wine.bin} ({game.wine.type})")
    line()
