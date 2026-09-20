"""ModSync-native MO2 setup — the "second machine" path.

Where MO2-LINT does a from-scratch guided install (Proton prefix configuration,
SKSE, a Steam redirector), this backend handles ModSync's common case: the game
is already installed (so its Proton prefix exists) and the mod content has synced
in. It writes a machine-local ModOrganizer.ini and a durable Proton launch — no
protontricks, no redirector, no Steam launch option to be wiped from appinfo.vdf.

Prototype scope: assumes the MO2 binaries are already present in the instance
(synced, or a prior install). Auto-downloading a portable MO2 when absent is a
follow-up.
"""

from __future__ import annotations

from pathlib import Path

from modsync import platforms
from modsync.games import Game
from modsync.mo2 import ini, launch
from modsync.mo2.installers.base import InstallerBackend, InstallResult, OnOutput
from modsync.steam import libraries

LAUNCH_SCRIPT = "modsync-mo2.sh"


class NativeBackend(InstallerBackend):
    name = "ModSync native"

    def available(self) -> tuple[bool, str]:
        return True, ""  # no external installer / protontricks required

    def _resolve_game(self, game: Game) -> tuple[Path, Path] | None:
        """``(steam_root, game_install_path)`` for the game, or ``None``."""
        for root in platforms.current().steam_roots():
            app = libraries.find_app(libraries.read_libraries(root), game.appid)
            if app is not None and app.install_path.exists():
                return root, app.install_path
        return None

    @staticmethod
    def _pick_profile(dest_dir: Path) -> str:
        profiles = dest_dir / "profiles"
        if profiles.is_dir():
            names = sorted(p.name for p in profiles.iterdir() if p.is_dir())
            if names:
                return "Default" if "Default" in names else names[0]
        return "Default"

    def install(
        self,
        game: Game,
        dest_dir: Path | str,
        *,
        script_extender: bool = False,
        on_output: OnOutput | None = None,
    ) -> InstallResult:
        dest_dir = Path(dest_dir)
        log = on_output or (lambda _msg: None)

        if not (dest_dir / "ModOrganizer.exe").exists():
            return InstallResult(
                False, 1, None,
                "No ModOrganizer.exe in the instance yet — sync it (or install MO2 "
                "there) first. Auto-download is a follow-up.",
            )

        resolved = self._resolve_game(game)
        if resolved is None:
            return InstallResult(False, 1, None, f"{game.name} isn't installed via Steam here.")
        steam_root, game_path = resolved
        log(f"Found {game.name}: {game_path}")

        if launch.find_proton(steam_root, game.appid) is None:
            return InstallResult(
                False, 1, None,
                "No Proton prefix for the game yet — run it once through Steam first.",
            )

        profile = self._pick_profile(dest_dir)
        ini_path = ini.write_local_ini(
            dest_dir, game_name=game.mo2_game_name, game_path=game_path, profile=profile
        )
        log(f"Wrote {ini_path.name} (profile: {profile})")

        script_path = dest_dir / LAUNCH_SCRIPT
        script_path.write_text(
            launch.launch_script(steam_root, game.appid, dest_dir / "ModOrganizer.exe"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        log(f"Wrote {script_path.name}")

        return InstallResult(True, 0, dest_dir, f"Launch MO2 with: {script_path}")
