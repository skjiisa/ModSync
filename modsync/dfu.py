"""Daggerfall Unity, set up from the Steam copy of Daggerfall.

Daggerfall Unity (DFU) is a native Linux game that needs the data files of
the original Daggerfall, which Steam gives away as app 1812390: a DOSBox
install whose ``DF/DAGGER`` folder holds ``ARENA2``. Nothing here touches MO2.

The install is the official Linux release, pinned by checksum and unpacked
unmodified (DFU's author supports no third-party builds, so ModSync never
patches it). Two files make it ours to manage:

* ``Portable.txt`` next to the program makes DFU keep settings, saves and the
  mod load order in ``PortableAppdata/`` inside the install instead of
  ``~/.config/unity3d``, so everything a machine needs lives in one folder.
* ``PortableAppdata/settings.ini`` gets one key, ``MyDaggerfallPath``, pointing
  at Steam's data. DFU fills in every other setting from its defaults. Its
  startup screen (``ShowOptionsAtStart``) is left on: that's where the mod list
  and DFU's settings are edited.

Mods go in ``DaggerfallUnity_Data/StreamingAssets/Mods``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from modsync import platforms
from modsync.downgrade import engine
from modsync.steam import libraries as libs
from modsync.steam import shortcuts

log = logging.getLogger(__name__)

DAGGERFALL_APPID = 1812390  # The Elder Scrolls II: Daggerfall (free on Steam)

DFU_VERSION = "1.1.1"
# v1.1.1-cve-2025 repackaged the Windows and macOS players for Unity's 2025
# security fix; its Linux zip is the 1.1.1 one.
DFU_URL = (
    "https://github.com/Interkarma/daggerfall-unity/releases/download"
    "/v1.1.1-cve-2025/dfu_linux_64bit-v1.1.1.zip"
)
DFU_SHA256 = "be3c476ae92972f42448757719266ad82c04868eb7b6f3a3c90fcfcf1490849a"

EXECUTABLE = "DaggerfallUnity.x86_64"
SHORTCUT_NAME = "Daggerfall Unity"
RECORD_NAME = "modsync-dfu.json"  # which DFU version ModSync put here

# DFU's DFValidator: what makes a folder a usable ARENA2.
_MIN_TEXTURES = 472
_REQUIRED_FILES = ("ARCH3D.BSA", "BLOCKS.BSA", "MAPS.BSA", "DAGGER.SND", "WOODS.WLD")


class DfuError(RuntimeError):
    pass


def default_install_dir() -> Path:
    return Path.home() / "Games" / "Daggerfall Unity"


# --- Steam's Daggerfall data --------------------------------------------------


def steam_daggerfall() -> Path | None:
    """The ``DF/DAGGER`` folder of Steam's Daggerfall, if Steam installed it."""
    app = libs.find_app(libs.all_libraries(platforms.current().steam_roots()), DAGGERFALL_APPID)
    if app is None:
        return None
    dagger = app.install_path / "DF" / "DAGGER"
    return dagger if dagger.is_dir() else None


def arena2(dagger: Path) -> Path | None:
    """``ARENA2`` under ``dagger``, in either case, as DFU looks for it."""
    for name in ("arena2", "ARENA2"):
        if (dagger / name).is_dir():
            return dagger / name
    return None


def data_problems(dagger: Path | None) -> list[str]:
    """Why DFU would reject ``dagger`` as ``MyDaggerfallPath``; empty when it
    wouldn't. Mirrors DFU's own check, which matches names exactly."""
    if dagger is None:
        return ["Steam hasn't installed The Elder Scrolls II: Daggerfall (it's free)."]
    folder = arena2(dagger)
    if folder is None:
        return [f"There's no ARENA2 folder in {dagger}."]
    try:
        names = {p.name for p in folder.iterdir()}
    except OSError as exc:
        return [f"Can't read {folder}: {exc}"]
    problems = []
    textures = sum(1 for n in names if n.startswith("TEXTURE.") and len(n) == len("TEXTURE.000"))
    if textures < _MIN_TEXTURES:
        problems.append(f"{folder} has {textures} TEXTURE files; DFU needs at least {_MIN_TEXTURES}.")
    missing = [n for n in _REQUIRED_FILES if n not in names]
    if missing:
        problems.append(f"{folder} is missing {', '.join(missing)}.")
    return problems


# --- the install ----------------------------------------------------------------


@dataclass
class Install:
    path: Path

    @property
    def executable(self) -> Path:
        return self.path / EXECUTABLE

    @property
    def data_dir(self) -> Path:
        return self.path / "PortableAppdata"

    @property
    def settings_ini(self) -> Path:
        return self.data_dir / "settings.ini"

    @property
    def mods_dir(self) -> Path:
        return self.path / "DaggerfallUnity_Data" / "StreamingAssets" / "Mods"

    @property
    def icon(self) -> Path:
        return self.path / "DaggerfallUnity_Data" / "Resources" / "UnityPlayer.png"

    @property
    def portable(self) -> bool:
        return (self.path / "Portable.txt").exists()

    @property
    def version(self) -> str | None:
        """The DFU version ModSync installed here; None for an install it didn't make."""
        try:
            return json.loads((self.path / RECORD_NAME).read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            return None

    def daggerfall_path(self) -> str | None:
        return read_daggerfall_path(self.settings_ini)


def find_install(path: Path | str | None) -> Install | None:
    """The DFU install at ``path``, if there is one."""
    if not path:
        return None
    inst = Install(Path(path))
    return inst if inst.executable.is_file() else None


def is_running() -> bool:
    # comm is cut to 15 characters, which is exactly "DaggerfallUnity".
    return shortcuts.process_running(EXECUTABLE[:15])


def read_daggerfall_path(ini: Path) -> str | None:
    try:
        lines = ini.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    section = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
        elif section == "Daggerfall" and "=" in stripped:
            key, _, value = stripped.partition("=")
            if key.strip() == "MyDaggerfallPath":
                return value.strip() or None
    return None


def write_daggerfall_path(ini: Path, dagger: Path) -> None:
    """Set ``[Daggerfall] MyDaggerfallPath`` in ``ini``, leaving every other
    line as it is. DFU writes the file in ``Key = Value`` form."""
    try:
        lines = ini.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    entry = f"MyDaggerfallPath = {dagger}"
    section = ""
    header = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            if section == "Daggerfall":
                header = i
        elif section == "Daggerfall" and stripped.partition("=")[0].strip() == "MyDaggerfallPath":
            lines[i] = entry
            break
    else:
        if header is None:
            lines[:0] = ["[Daggerfall]", entry, ""] if lines else ["[Daggerfall]", entry]
        else:
            lines.insert(header + 1, entry)
    ini.parent.mkdir(parents=True, exist_ok=True)
    tmp = ini.with_name(f".{ini.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, ini)


def configure(inst: Install, dagger: Path) -> None:
    """Make ``inst`` portable and point it at ``dagger``. Safe to repeat, and
    how an install follows Daggerfall to another Steam library."""
    if is_running():
        # DFU writes settings.ini back when it exits, over this change.
        raise DfuError("Close Daggerfall Unity first; it would undo the change when it exits.")
    (inst.path / "Portable.txt").touch()
    write_daggerfall_path(inst.settings_ini, dagger)
    log.info("pointed Daggerfall Unity in %s at %s", inst.path, dagger)


def _fetch(cache_dir: Path, progress: engine.ProgressFn | None) -> Path:
    try:
        return engine.fetch_verified(
            DFU_URL, cache_dir / DFU_URL.rsplit("/", 1)[1], DFU_SHA256, f"Daggerfall Unity {DFU_VERSION}", progress,
        )
    except engine.DowngradeError as exc:
        raise DfuError(str(exc)) from exc


def _extract(archive: Path, dest: Path, progress: engine.ProgressFn | None) -> None:
    """Unpack the release zip into ``dest``, keeping the executable bits
    (``zipfile`` drops them) and refusing entries that would land outside."""
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        members = zf.infolist()
        for n, info in enumerate(members, 1):
            target = (dest / info.filename).resolve()
            mode = info.external_attr >> 16
            if (target != root and root not in target.parents) or stat.S_ISLNK(mode):
                raise DfuError(f"The Daggerfall Unity archive has an unsafe entry: {info.filename}")
            zf.extract(info, dest)
            if mode & 0o777 and not info.is_dir():
                target.chmod(mode & 0o777)
            if progress and (n % 200 == 0 or n == len(members)):
                progress(engine.Progress("extract", "Unpacking Daggerfall Unity", n, len(members)))


@dataclass
class Installed:
    install: Install
    dagger: Path
    fresh: bool  # False when an install was already there and only re-pointed
    notes: list[str] = field(default_factory=list)


def install(
    dest: Path | str,
    dagger: Path,
    *,
    cache_dir: Path,
    progress: engine.ProgressFn | None = None,
) -> Installed:
    """Install DFU into ``dest`` and point it at ``dagger``.

    ``dest`` must be missing or empty, or hold a DFU that ModSync installed at
    this version, which is only re-pointed (mods, saves and settings stay)."""
    dest = Path(dest).expanduser().absolute()
    problems = data_problems(dagger)
    if problems:
        raise DfuError(" ".join(problems))
    if is_running():
        raise DfuError("Close Daggerfall Unity first.")
    existing = find_install(dest)
    if existing is not None:
        if existing.version != DFU_VERSION:
            found = f"Daggerfall Unity {existing.version}" if existing.version else "a Daggerfall Unity ModSync didn't install"
            raise DfuError(f"{dest} already has {found}. Choose an empty folder.")
        configure(existing, dagger)
        return Installed(existing, dagger, fresh=False)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise DfuError(f"{dest} isn't empty. Choose an empty or new folder.")

    archive = _fetch(cache_dir, progress)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unpack beside the destination and move it into place whole, so a failed
    # install never leaves half a game where the user asked for one.
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}.modsync-"))
    try:
        tmp.chmod(0o755)  # mkdtemp makes it private; this becomes the game folder
        _extract(archive, tmp, progress)
        if not (tmp / EXECUTABLE).is_file():
            raise DfuError(f"The Daggerfall Unity archive has no {EXECUTABLE}.")
        (tmp / RECORD_NAME).write_text(json.dumps({"version": DFU_VERSION}, indent=2) + "\n", encoding="utf-8")
        if dest.exists():
            dest.rmdir()
        tmp.replace(dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    inst = Install(dest)
    configure(inst, dagger)
    log.info("installed Daggerfall Unity %s into %s", DFU_VERSION, dest)
    return Installed(inst, dagger, fresh=True)


# --- Steam ------------------------------------------------------------------------


def add_shortcut(inst: Install) -> list[Path]:
    """A "Daggerfall Unity" non-Steam game that runs DFU natively. Steam must
    be closed, and restarted afterwards to show it."""
    icon = str(inst.icon) if inst.icon.exists() else ""
    return shortcuts.add_to_steam(
        SHORTCUT_NAME, exe=f'"{inst.executable}"', start_dir=f'"{inst.path}"', icon=icon,
    )


def remove_shortcut() -> list[Path]:
    return shortcuts.remove_from_steam(SHORTCUT_NAME)


def has_shortcut() -> bool:
    return shortcuts.has_shortcut(SHORTCUT_NAME)
