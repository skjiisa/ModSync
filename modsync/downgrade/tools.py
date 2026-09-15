"""Locate the external tools a downgrade needs: ``xdelta3`` and a 7z extractor.

Both are tiny, ubiquitous C programs. The Flatpak bundles them; on a plain
distro they come from the package manager (``xdelta3``, ``7zip``/``p7zip``),
and ``bsdtar`` (libarchive, present on SteamOS) can read 7z too. Override the
xdelta3 binary with ``MODSYNC_XDELTA3`` if needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ToolError(RuntimeError):
    pass


def find_xdelta3() -> Path | None:
    env = os.environ.get("MODSYNC_XDELTA3")
    if env and Path(env).is_file():
        return Path(env)
    for cand in (shutil.which("xdelta3"), Path.home() / ".local/bin/xdelta3", Path("/app/bin/xdelta3")):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def xdelta3_apply(xdelta3: Path, source: Path, patch: Path, output: Path) -> None:
    """``xdelta3 -d -s source patch output``. xdelta3 checks the source's
    checksum, so patching the wrong version fails instead of producing junk."""
    output.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [str(xdelta3), "-d", "-f", "-s", str(source), str(patch), str(output)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        output.unlink(missing_ok=True)
        raise ToolError(f"xdelta3 failed on {source.name}: {(res.stderr or res.stdout).strip()}")


@dataclass(frozen=True)
class Extractor:
    name: str
    argv: tuple[str, ...]  # with {archive} and {dest} placeholders

    def extract(self, archive: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        cmd = [a.format(archive=str(archive), dest=str(dest)) for a in self.argv]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise ToolError(f"{self.name} failed on {archive.name}: {(res.stderr or res.stdout).strip()[-400:]}")


def find_extractor() -> Extractor | None:
    for name in ("7z", "7zz", "7za", "7zr"):
        path = shutil.which(name)
        if path:
            return Extractor(name, (path, "x", "-y", "-bso0", "-bsp0", "-o{dest}", "{archive}"))
    path = shutil.which("bsdtar")
    if path:
        return Extractor("bsdtar", (path, "-xf", "{archive}", "-C", "{dest}"))
    try:
        import py7zr  # type: ignore  # noqa: F401
    except ImportError:
        return None
    import sys

    code = "import sys, py7zr; py7zr.SevenZipFile(sys.argv[1]).extractall(sys.argv[2])"
    return Extractor("py7zr", (sys.executable, "-c", code, "{archive}", "{dest}"))


def missing_tools() -> list[str]:
    missing = []
    if find_xdelta3() is None:
        missing.append("xdelta3")
    if find_extractor() is None:
        missing.append("7z (or bsdtar / py7zr)")
    return missing
