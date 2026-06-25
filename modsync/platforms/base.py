"""The platform seam. Implementations isolate the OS-specific bits so that adding
Windows later is one new module rather than a rewrite."""

from __future__ import annotations

import abc
from pathlib import Path


class Platform(abc.ABC):
    name: str = "unknown"

    @abc.abstractmethod
    def steam_roots(self) -> list[Path]:
        """Candidate Steam root dirs that actually exist, canonicalised."""

    @abc.abstractmethod
    def mo2_broad_roots(self) -> list[Path]:
        """Dirs to search broadly for MO2 instances (shallow; hidden dirs pruned)."""

    @abc.abstractmethod
    def mo2_known_roots(self) -> list[Path]:
        """Specific known install locations (searched deeply, hidden paths allowed)."""
