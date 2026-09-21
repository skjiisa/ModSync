"""Install the SKSE build that matches the installed game version.

SKSE is compiled against one exact Skyrim runtime and won't load on any
other, so after a downgrade — or on a machine that copied its mods from
another one — the SKSE in the game folder is usually the wrong one, and the
Game card can only say so. This does the fix: download the matching build,
verify it, drop the old ``skse64_*`` files from the game folder and copy the
new loader, runtime DLL and ``Data/Scripts`` in — exactly what a hand install
from skse.silverlock.org does, and what MO2-LINT does when it can pick the
right build.

The build table is deliberately small: each entry was downloaded and
checksummed by hand (2.0.20 and 2.2.6 also match MO2-LINT's ``game_info.yml``).
Builds published only on Nexus (which needs a login) get a page link instead
of a URL; the UI opens it and the user installs by hand.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from modsync.downgrade import engine, tools
from modsync.gameversion import GameVersion

log = logging.getLogger(__name__)

SKSE_PAGE = "https://skse.silverlock.org/"


class SkseError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkseBuild:
    version: str  # SKSE's own version, e.g. "2.2.6"
    runtime: str  # the one Skyrim SE runtime it's built for
    url: str | None  # direct .7z download; None when only a manual page exists
    sha256: str
    subdir: str  # top-level folder inside the archive
    page: str = SKSE_PAGE

    @property
    def downloadable(self) -> bool:
        return self.url is not None

    @property
    def dll_name(self) -> str:
        return "skse64_" + self.runtime.replace(".", "_") + ".dll"


BUILDS: tuple[SkseBuild, ...] = (
    SkseBuild(
        "2.0.20", "1.5.97",
        "https://skse.silverlock.org/beta/skse64_2_00_20.7z",
        "46f70b963b22e3c242bac45e1716c39349798fba5b74c978419a10d604b542d7",
        "skse64_2_00_20",
    ),
    SkseBuild(
        "2.2.3", "1.6.640",
        "https://skse.silverlock.org/beta/skse64_2_02_03.7z",
        "4fd9cebcc629a816c0783f78cca33df438e9b1ac8108cf3d83706212106a701c",
        "skse64_2_02_03",
    ),
    SkseBuild(
        "2.2.6", "1.6.1170",
        "https://skse.silverlock.org/beta/skse64_2_02_06.7z",
        "d7297f1a1d613e5265e1af4dbbfe8bd37a32719c1ccef363fc6187fa6eba0848",
        "skse64_2_02_06",
    ),
    # Current builds are published on Nexus only (login required).
    SkseBuild(
        "2.3.1", "1.7.104", None, "", "",
        page="https://www.nexusmods.com/skyrimspecialedition/mods/30379?tab=files",
    ),
)


def build_for(runtime: GameVersion | str | None) -> SkseBuild | None:
    if runtime is None:
        return None
    wanted = str(runtime)
    for b in BUILDS:
        if b.runtime == wanted:
            return b
    return None


# --- the install ------------------------------------------------------------

# What a build puts in the game folder. Everything else in the archive (src/,
# .pdb, readme) stays out, like MO2-LINT's file whitelist.
_ROOT_GLOBS = ("skse64_*.dll", "skse64_loader.exe")  # the .dll glob covers skse64_steam_loader.dll too


def _root_files(folder: Path) -> list[Path]:
    found: dict[Path, None] = {}
    for pattern in _ROOT_GLOBS:
        for f in sorted(folder.glob(pattern)):
            found[f] = None
    return list(found)


@dataclass
class Installed:
    build: SkseBuild
    game_dir: Path
    removed: list[str] = field(default_factory=list)  # old SKSE files taken out of the game folder
    files: int = 0  # files copied in


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(build: SkseBuild, cache_dir: Path, progress: engine.ProgressFn | None) -> Path:
    assert build.url is not None
    archive = cache_dir / (build.subdir + ".7z")
    if archive.exists() and _sha256(archive) == build.sha256:
        return archive
    archive.unlink(missing_ok=True)
    try:
        engine._download(build.url, archive, progress, f"SKSE {build.version}")
    except engine.DowngradeError as exc:
        raise SkseError(str(exc)) from exc
    if _sha256(archive) != build.sha256:
        archive.unlink(missing_ok=True)
        raise SkseError(f"SKSE {build.version} download didn't match its checksum; deleted it, please retry")
    return archive


def installed_files(game_dir: Path) -> list[Path]:
    """The SKSE files currently in the game folder root."""
    return _root_files(game_dir)


def install(
    build: SkseBuild,
    game_dir: Path | str,
    cache_dir: Path | str,
    progress: engine.ProgressFn | None = None,
) -> Installed:
    """Put ``build`` into ``game_dir``, replacing whatever SKSE was there."""
    if not build.downloadable:
        raise SkseError(
            f"SKSE {build.version} for Skyrim {build.runtime} isn't available for direct "
            f"download; get it from {build.page} and unpack it into the game folder."
        )
    game_dir = Path(game_dir)
    cache_dir = Path(cache_dir)
    if not (game_dir / "SkyrimSE.exe").exists():
        raise SkseError(f"{game_dir} doesn't look like a Skyrim SE install (no SkyrimSE.exe)")
    extractor = tools.find_extractor()
    if extractor is None:
        raise SkseError("no 7z extractor found (install 7zip, or bsdtar)")
    cache_dir.mkdir(parents=True, exist_ok=True)

    archive = _fetch(build, cache_dir, progress)
    result = Installed(build, game_dir)
    with tempfile.TemporaryDirectory(prefix="skse-", dir=cache_dir) as tmp:
        if progress:
            progress(engine.Progress("extract", f"SKSE {build.version}"))
        extractor.extract(archive, Path(tmp))
        src = Path(tmp) / build.subdir
        if not (src / "skse64_loader.exe").exists() or not (src / build.dll_name).exists():
            raise SkseError(f"the SKSE {build.version} archive doesn't contain the expected files")

        # Out with the old: leftover DLLs for another runtime would make the
        # version check report several SKSEs and MO2 might launch the wrong one.
        for old in installed_files(game_dir):
            old.unlink()
            result.removed.append(old.name)
            log.info("removed old SKSE file %s", old)

        if progress:
            progress(engine.Progress("install", f"SKSE {build.version}"))
        for f in _root_files(src):
            shutil.copy2(f, game_dir / f.name)
            result.files += 1
        data = src / "Data"
        if data.is_dir():
            for f in data.rglob("*"):
                if f.is_file():
                    dest = game_dir / "Data" / f.relative_to(data)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    result.files += 1
    log.info(
        "installed SKSE %s for %s into %s (%d files, removed %s)",
        build.version, build.runtime, game_dir, result.files, result.removed or "nothing",
    )
    return result
