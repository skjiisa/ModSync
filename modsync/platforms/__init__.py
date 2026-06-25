"""Platform selection. ``current()`` returns the right backend for this OS.

Named ``platforms`` (not ``platform``) to avoid shadowing the stdlib module.
"""

from __future__ import annotations

import sys

from modsync.platforms.base import Platform
from modsync.platforms.linux import LinuxPlatform

__all__ = ["Platform", "LinuxPlatform", "current"]


def current() -> Platform:
    if sys.platform.startswith("win"):
        # from modsync.platforms.windows import WindowsPlatform
        # return WindowsPlatform()
        raise NotImplementedError("Windows support is planned for post-1.0")
    return LinuxPlatform()
