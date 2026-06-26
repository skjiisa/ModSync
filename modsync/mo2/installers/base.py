"""Installer backend interface. ModSync stays installer-agnostic: each backend
sets up a portable MO2 instance and reports a uniform result."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from modsync.games import Game

OnOutput = Callable[[str], None]


@dataclass
class InstallResult:
    success: bool
    returncode: int
    instance_path: Path | None = None
    message: str = ""


class InstallerBackend(abc.ABC):
    name: str = "installer"

    @abc.abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return ``(ok, reason)`` — ``reason`` explains what's missing if not ok."""

    @abc.abstractmethod
    def install(
        self,
        game: Game,
        dest_dir: Path | str,
        *,
        script_extender: bool = False,
        on_output: OnOutput | None = None,
    ) -> InstallResult:
        """Install a portable MO2 instance for ``game`` into ``dest_dir``.

        ``script_extender`` defaults off: MO2-LINT v7.0.0-rc5's auto-SKSE step
        crashes for Skyrim SE (KeyError matching the game version). The core
        instance install is unaffected, so we leave SKSE as a later/manual step.
        """
