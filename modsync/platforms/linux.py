"""Linux / SteamOS platform implementation."""

from __future__ import annotations

from pathlib import Path

from modsync.platforms.base import Platform


def _existing(candidates: list[Path]) -> list[Path]:
    """The candidates that exist, canonicalised, each once."""
    found: list[Path] = []
    for c in candidates:
        try:
            if not c.exists():
                continue
            resolved = c.resolve()
        except OSError:
            continue
        if resolved not in found:
            found.append(resolved)
    return found


class LinuxPlatform(Platform):
    name = "linux"

    def steam_roots(self) -> list[Path]:
        home = Path.home()
        return _existing([
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".steam/root",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ])

    def heroic_config_dirs(self) -> list[Path]:
        # Under the home folder, not XDG_CONFIG_HOME: inside ModSync's Flatpak
        # that points at ModSync's own config.
        home = Path.home()
        return _existing([
            home / ".config/heroic",
            home / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
        ])

    def mo2_broad_roots(self) -> list[Path]:
        return [Path.home()]

    def mo2_known_roots(self) -> list[Path]:
        home = Path.home()
        return [
            home / "ModOrganizer2",                  # Jackify default install dir
            home / ".config/steamtinkerlaunch/MO2",  # Steam Tinker Launch MO2 area
        ]
