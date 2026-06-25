"""Linux / SteamOS platform implementation."""

from __future__ import annotations

from pathlib import Path

from modsync.platforms.base import Platform


class LinuxPlatform(Platform):
    name = "linux"

    def steam_roots(self) -> list[Path]:
        home = Path.home()
        candidates = [
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".steam/root",
            home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ]
        roots: list[Path] = []
        seen: set[Path] = set()
        for c in candidates:
            try:
                if not c.exists():
                    continue
                resolved = c.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            roots.append(resolved)
        return roots

    def mo2_broad_roots(self) -> list[Path]:
        return [Path.home()]

    def mo2_known_roots(self) -> list[Path]:
        home = Path.home()
        return [
            home / "ModOrganizer2",                  # Jackify default install dir
            home / ".config/steamtinkerlaunch/MO2",  # Steam Tinker Launch MO2 area
        ]
