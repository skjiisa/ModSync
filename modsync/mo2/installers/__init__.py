"""Guided MO2 install backends (MO2-LINT now; Jackify/manual later)."""

from modsync.mo2.installers.base import InstallerBackend, InstallResult
from modsync.mo2.installers.mo2lint import Mo2LintBackend

__all__ = ["InstallerBackend", "InstallResult", "Mo2LintBackend"]
