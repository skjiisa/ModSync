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

Mods go in ``DaggerfallUnity_Data/StreamingAssets/Mods``. Steam's own Play
button on Daggerfall can start it, through the launch hook
(:mod:`modsync.launchhook`).

:data:`MODS_FOLDER` syncs the mods between machines (see
:mod:`modsync.sync.folders`): one Syncthing folder rooted at the install,
allowing only what users add (the folders under ``StreamingAssets`` that DFU
ships empty but for a readme) and ``PortableAppdata/Mods``, where DFU keeps
the load order (``GameData/Mods.json``) and each mod's settings. Everything
DFU ships itself, its ``settings.ini`` and key bindings, and saves stay out.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field

try:
    import fcntl
except ImportError:  # pragma: no cover - not on Windows
    fcntl = None
from pathlib import Path

from modsync import background, launchhook, platforms
from modsync.downgrade import engine
from modsync.games import DAGGERFALL
from modsync.state import State
from modsync.sync.folders import FolderSpec
from modsync.steam import libraries as libs
from modsync.steam import shortcuts

log = logging.getLogger(__name__)

DAGGERFALL_APPID = DAGGERFALL.appid  # The Elder Scrolls II: Daggerfall (free on Steam)

DFU_VERSION = "1.1.1"
# v1.1.1-cve-2025 repackaged the Windows and macOS players for Unity's 2025
# security fix; its Linux zip is the 1.1.1 one.
DFU_URL = (
    "https://github.com/Interkarma/daggerfall-unity/releases/download"
    "/v1.1.1-cve-2025/dfu_linux_64bit-v1.1.1.zip"
)
DFU_SHA256 = "be3c476ae92972f42448757719266ad82c04868eb7b6f3a3c90fcfcf1490849a"

EXECUTABLE = "DaggerfallUnity.x86_64"
RECORD_NAME = "modsync-dfu.json"  # which DFU version ModSync put here; never synced
VAULT_RECORD = "modsync-dfu-vault.json"  # which DFU version the synced mods are for; synced

# The StreamingAssets folders a release ships with nothing but a readme: what
# users add. The others (Quests, Text, Tables, Fonts, ...) are DFU's own and
# must stay as each machine's version installed them.
USER_ASSET_DIRS = ("Mods", "Textures", "Sound", "Books", "Movies", "Presets", "QuestPacks", "SoundFonts",
                   "WorldData")
_ASSETS = "DaggerfallUnity_Data/StreamingAssets"
SYNCED_DIRS = (*(f"{_ASSETS}/{d}" for d in USER_ASSET_DIRS), "PortableAppdata/Mods")

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
    def portable(self) -> bool:
        return (self.path / "Portable.txt").exists()

    @property
    def recorded_version(self) -> str | None:
        """The DFU version ModSync installed here; None for an install it didn't make."""
        try:
            return json.loads((self.path / RECORD_NAME).read_text(encoding="utf-8")).get("version")
        except (OSError, ValueError, AttributeError):
            return None

    @property
    def version(self) -> str | None:
        """This install's DFU version: the one ModSync installed, else the one
        the build says it is. None when neither tells."""
        return self.recorded_version or built_version(self.path)

    @property
    def windows_build(self) -> bool:
        return (self.path / "DaggerfallUnity.exe").is_file() and not self.executable.is_file()

    def daggerfall_path(self) -> str | None:
        return read_daggerfall_path(self.settings_ini)


def find_install(path: Path | str | None) -> Install | None:
    """The DFU install at ``path``, if there is one."""
    if not path:
        return None
    inst = Install(Path(path))
    return inst if inst.executable.is_file() else None


# DFU's options window prints VersionInfo's product name, status and version
# ("Daggerfall Unity Release 1.1.1"), so the three sit together among the
# assembly's UTF-16 strings, one length or end byte apart.
_BUILT_VERSION = re.compile(r"Daggerfall Unity(?:.[A-Za-z ]{1,16})?.(\d+\.\d+\.\d+)", re.DOTALL)
_built_versions: dict[tuple[str, int, int], str | None] = {}


def built_version(path: Path) -> str | None:
    """The version a DFU build at ``path`` says it is, read from its game code.
    None when it can't tell."""
    dll = path / "DaggerfallUnity_Data" / "Managed" / "Assembly-CSharp.dll"
    try:
        st = dll.stat()
    except OSError:
        return None
    key = (str(dll), st.st_mtime_ns, st.st_size)
    if key not in _built_versions:
        try:
            data = dll.read_bytes()
        except OSError:
            return None
        found = None
        for start in (0, 1):  # a string can start at either byte
            chunk = data[start:]
            match = _BUILT_VERSION.search(chunk[:len(chunk) // 2 * 2].decode("utf-16-le", "replace"))
            if match:
                found = match.group(1)
                break
        _built_versions[key] = found
    return _built_versions[key]


def is_running() -> bool:
    # comm is cut to 15 characters, which is exactly "DaggerfallUnity".
    return shortcuts.process_running(EXECUTABLE[:15])


def read_setting(ini: Path, section: str, key: str) -> str | None:
    try:
        lines = ini.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    current = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
        elif current == section and "=" in stripped:
            name, _, value = stripped.partition("=")
            if name.strip() == key:
                return value.strip() or None
    return None


def write_settings(ini: Path, section: str, values: dict[str, object]) -> None:
    """Set ``values`` under ``[section]`` in ``ini``, leaving every other line
    as it is. DFU writes the file in ``Key = Value`` form."""
    try:
        lines = ini.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        lines = []
    for key, value in values.items():
        entry = f"{key} = {value}"
        current = ""
        header = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                current = stripped[1:-1].strip()
                if current == section:
                    header = i
            elif current == section and stripped.partition("=")[0].strip() == key:
                lines[i] = entry
                break
        else:
            if header is not None:
                lines.insert(header + 1, entry)
            else:
                lines += ["", f"[{section}]", entry] if lines else [f"[{section}]", entry]
    ini.parent.mkdir(parents=True, exist_ok=True)
    tmp = ini.with_name(f".{ini.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, ini)


def read_daggerfall_path(ini: Path) -> str | None:
    return read_setting(ini, "Daggerfall", "MyDaggerfallPath")


def write_daggerfall_path(ini: Path, dagger: Path) -> None:
    write_settings(ini, "Daggerfall", {"MyDaggerfallPath": dagger})


_XRANDR_OUTPUT = re.compile(r"^\S+ connected (primary )?(\d+)x(\d+)\+", re.MULTILINE)


def display_resolution() -> tuple[int, int] | None:
    """The primary display's current mode (the first display's when none is
    primary), which DFU's own first-run resolution step would offer. None
    when ``xrandr`` can't tell."""
    try:
        result = background.run_host(["xrandr", "--current"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    outputs = [(bool(m[1]), int(m[2]), int(m[3])) for m in _XRANDR_OUTPUT.finditer(result.stdout)]
    if not outputs:
        return None
    _, width, height = next((o for o in outputs if o[0]), outputs[0])
    return width, height


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
    resolution: tuple[int, int] | None = None,
) -> Installed:
    """Install DFU into ``dest`` and point it at ``dagger``.

    ``dest`` must be missing or empty, or hold a DFU that ModSync installed at
    this version, which is only re-pointed (mods, saves and settings stay).

    A fresh install also gets ``resolution``, normally the display's. With
    the data path already valid, DFU skips the first-run step that would
    have asked, and falls back to 1920x1080, which a screen of another shape
    shows stretched."""
    dest = Path(dest).expanduser().absolute()
    problems = data_problems(dagger)
    if problems:
        raise DfuError(" ".join(problems))
    if is_running():
        raise DfuError("Close Daggerfall Unity first.")
    existing = find_install(dest)
    if existing is not None:
        if existing.recorded_version != DFU_VERSION:
            found = (f"Daggerfall Unity {existing.recorded_version}" if existing.recorded_version
                     else "a Daggerfall Unity ModSync didn't install")
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
    if resolution:
        width, height = resolution
        write_settings(inst.settings_ini, "Video", {"ResolutionWidth": width, "ResolutionHeight": height})
    log.info("installed Daggerfall Unity %s into %s (resolution %s)", DFU_VERSION, dest, resolution or "DFU's default")
    return Installed(inst, dagger, fresh=True)


# --- an install ModSync didn't make -------------------------------------------------


def unity_data_dir() -> Path:
    """Where a DFU without ``Portable.txt`` keeps its settings, saves and load
    order: Unity's folder for the game under the host's ``~/.config``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    config = Path(base) if base and not background.in_flatpak() else Path.home() / ".config"
    return config / "unity3d" / "Daggerfall Workshop" / "Daggerfall Unity"


# Unity's own files there, which it keeps using whether DFU is portable or not.
_UNITY_OWN = ("prefs", "Player.log", "Player-prev.log")


@dataclass
class Existing:
    """A DFU that ModSync didn't install, as adopting it would find it."""

    install: Install
    data_from: Path | None  # DFU's data in ~/.config, copied in because the install isn't portable


def _unity_data(source: Path) -> list[Path]:
    try:
        return [p for p in source.iterdir() if p.name not in _UNITY_OWN]
    except OSError:
        return []


def examine(path: Path | str) -> Existing:
    """Check that ``path`` holds a DFU that ModSync can use, and what using it
    would change. Raises :class:`DfuError` when it can't be used."""
    path = Path(path).expanduser().absolute()
    inst = Install(path)
    if not inst.executable.is_file():
        if inst.windows_build:
            raise DfuError(f"{path} has the Windows build of Daggerfall Unity. ModSync works with the Linux "
                           "build: install that, or let ModSync install it.")
        raise DfuError(f"There's no Daggerfall Unity in {path}. Choose the folder with {EXECUTABLE} in it.")
    if not os.access(path, os.W_OK):
        raise DfuError(f"ModSync can't change {path}. Choose a Daggerfall Unity in a folder you own.")
    source = unity_data_dir()
    copies = not inst.portable and bool(_unity_data(source))
    return Existing(inst, source if copies else None)


def _copy_missing(source: Path, dest: Path) -> None:
    """Copy ``source`` into ``dest``, keeping whatever ``dest`` has already."""
    for entry in _unity_data(source):
        files = [entry] if entry.is_file() else [Path(top) / n for top, _dirs, names in os.walk(entry) for n in names]
        for src in files:
            target = dest / src.relative_to(source)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)


def adopt(path: Path | str) -> tuple[Existing, str | None]:
    """Use the DFU at ``path`` on this machine instead of installing one.

    ModSync syncs saves and the load order from inside the install, so an
    install that isn't portable is made portable, its data copied in from
    ``~/.config`` first (the originals stay, and nothing already in the
    install is replaced). Daggerfall's data it already uses is kept when DFU
    would accept it, else it gets Steam's. An enabled Play hook moves to it.
    Returns what was found and what the hook said, if anything."""
    found = examine(path)
    inst = found.install
    if is_running():
        # DFU writes settings.ini back when it exits, over these changes.
        raise DfuError("Close Daggerfall Unity first.")
    if found.data_from is not None:
        _copy_missing(found.data_from, inst.data_dir)
        log.info("copied Daggerfall Unity's data from %s into %s", found.data_from, inst.data_dir)
    (inst.path / "Portable.txt").touch()
    configured = inst.daggerfall_path()
    if not configured or data_problems(Path(configured)):
        dagger = steam_daggerfall()
        if not data_problems(dagger):
            write_daggerfall_path(inst.settings_ini, dagger)
    previous = current_install()
    if previous is not None and previous.path != inst.path:
        _release_saves()  # the saves library syncs with the new install's Saves from now on
    State.update(dfu_path=str(inst.path))
    log.info("using the Daggerfall Unity in %s (%s)", inst.path, inst.version or "version unknown")
    return found, follow_install(inst)


# --- Steam's Play button -----------------------------------------------------------


def hook_status() -> launchhook.LaunchHookStatus:
    return launchhook.status(DAGGERFALL_APPID)


def enable_hook(inst: Install) -> str:
    """Make Steam's Play button on Daggerfall start ``inst`` instead of the
    original game. Queued until Steam closes when it's running."""
    return launchhook.enable(DAGGERFALL_APPID, native=inst.executable)


def disable_hook() -> str:
    return launchhook.disable(DAGGERFALL_APPID)


def follow_install(inst: Install) -> str | None:
    """Point the hook at ``inst`` when Steam uses it (or is about to) for
    another copy. A queued disable, or another launcher picked in Steam,
    is left as it is: enabling would undo that choice."""
    st = hook_status()
    wanted = st.enabled or bool(st.pending and st.pending.action == "select")
    if not st.installed or not wanted or (st.pending and st.pending.action == "restore"):
        return None
    if st.native == str(inst.executable):
        return None
    return enable_hook(inst)


# --- syncing the mods -------------------------------------------------------------


def current_install() -> Install | None:
    """The install this machine uses: the one ModSync recorded, else one
    ModSync made in the default folder (its record reset since). One there
    that ModSync didn't make (no record of its version, or not portable) is
    never taken: syncing it would change a folder nobody handed to ModSync.
    Everything that acts on "the" install goes through this."""
    recorded = find_install(State.load().dfu_path)
    if recorded is not None:
        return recorded
    default = find_install(default_install_dir())
    return default if default is not None and default.recorded_version and default.portable else None


def _install_root() -> Path | None:
    inst = current_install()
    return inst.path if inst else None


def _record_vault_version(root: Path) -> None:
    """The machine that starts syncing the mods says which DFU they're for."""
    record = root / VAULT_RECORD
    if not record.exists():
        version = Install(root).version or DFU_VERSION
        record.write_text(json.dumps({"dfu_version": version}, indent=2) + "\n", encoding="utf-8")


def _read_version(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("dfu_version")
    except (OSError, ValueError, AttributeError):
        return None


def vault_version(inst: Install) -> str | None:
    return _read_version(inst.path / VAULT_RECORD)


def vault_versions(inst: Install) -> set[str]:
    """Every version the synced records name: two machines that each started
    sharing before they met leave a conflict copy of the record too."""
    stem, ext = VAULT_RECORD.rsplit(".", 1)
    paths = [inst.path / VAULT_RECORD, *inst.path.glob(f"{stem}.sync-conflict-*.{ext}")]
    return {v for p in paths if (v := _read_version(p))}


def version_problem(inst: Install) -> str | None:
    """Why the synced mods may not suit this machine's DFU, or None."""
    here = inst.version
    others = sorted(v for v in vault_versions(inst) if v != here)
    if here and others:
        return (f"The synced mods are for Daggerfall Unity {', '.join(others)}, but this machine has {here}. "
                "Install the same version on every machine.")
    return None


SET_ASIDE_DIR = ".modsync-conflicts/set-aside"  # in the install; outside everything DFU reads


def set_aside_mod_copies(root: Path) -> list[Path]:
    """Move the conflict copies Syncthing leaves beside a ``.dfmod`` out of
    DFU's reach. DFU loads every ``.dfmod`` under ``Mods``, and of two with
    one title keeps whichever it finds first, so an older copy could load in
    place of the version in use. The version in use stays (the newer one,
    Syncthing's choice); the other goes to ``SET_ASIDE_DIR``, which nothing
    syncs. Returns where each went."""
    mods = root / _ASSETS / "Mods"
    moved = []
    for top, _dirs, files in os.walk(mods):
        for name in files:
            if ".sync-conflict-" not in name or not name.endswith(".dfmod"):
                continue
            source = Path(top) / name
            dest = root / SET_ASIDE_DIR / source.relative_to(root)
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, dest)
            moved.append(dest)
            log.info("set aside the other version of a mod: %s", dest)
    return moved


MODS_FOLDER = FolderSpec(
    key="dfu-mods",
    label="Daggerfall Unity mods (ModSync)",
    path=_install_root,
    ignore=(
        "// Managed by ModSync. Syncs Daggerfall Unity's mods, the files users add under",
        "// StreamingAssets, and the load order and mod settings in PortableAppdata/Mods.",
        "// Never the program, the files DFU ships itself, settings.ini, key bindings or saves.",
        "",
        *(f"!/{d}" for d in SYNCED_DIRS),
        f"!/{VAULT_RECORD}",
        "",
        "// Everything else. Syncthing still looks inside these for the folders above.",
        "/*",
    ),
    content=(*SYNCED_DIRS, VAULT_RECORD),
    on_share=_record_vault_version,
    in_use=lambda: is_running(),  # looked up when asked, so tests and callers see the live check
    tidy=lambda root: set_aside_mod_copies(root),
)


# --- syncing the saves (see modsync.dfu_saves) ------------------------------------


def saves_dir(inst: Install) -> Path:
    """Where DFU keeps this install's saves: ``MyDaggerfallUnitySavePath`` when
    it names a folder that exists (DFU uses it as it is), else
    ``PortableAppdata/Saves``."""
    custom = read_setting(inst.settings_ini, "Daggerfall", "MyDaggerfallUnitySavePath")
    if custom:
        path = Path(custom) if Path(custom).is_absolute() else inst.path / custom
        if path.is_dir():
            return path
    return inst.data_dir / "Saves"


SAVES_MARKER = ".modsync-saves"  # in the install: the Play hook syncs saves around DFU


def _machine() -> "dfu_saves.Machine":
    """This machine in the save versions it writes. Kept with ModSync, not
    with the saves, so a DFU install copied to another machine can't bring
    it along."""
    from modsync import config, dfu_saves

    return dfu_saves.Machine(config.data_dir() / "dfu-saves-machine.json")


def saves_library() -> Path:
    from modsync import config

    return config.data_dir() / "dfu-saves"


def _saves_root() -> Path | None:
    return saves_library() if _install_root() is not None else None


PLAYING_LOCK = ".modsync-playing"  # in the install: held by Steam's Play hook while DFU runs


@contextmanager
def _free_to_sync(inst: Install, playing: bool):
    """Entered around each change the saves sync makes: yields why it must
    stop ("" when it needn't). While Steam's Play hook has DFU starting or
    running, it holds :data:`PLAYING_LOCK`; otherwise this holds it shared,
    so the hook waits for the one change under way, never a whole sync,
    before it starts DFU. The hook's own syncs, just before and after DFU,
    say ``playing``."""
    if is_running():
        yield "Daggerfall Unity is running; its saves sync once it closes."
        return
    if playing or fcntl is None:
        yield ""
        return
    with open(inst.path / PLAYING_LOCK, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield "Daggerfall Unity is starting or running; its saves sync once it closes."
            return
        try:
            yield ""
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def sync_saves(*, force: bool = False, playing: bool = False) -> "dfu_saves.Report":
    """Bring this machine's saves and the synced library in line, when saves
    sync is on here (or ``force``) and DFU is closed."""
    from modsync import dfu_saves
    from modsync.sync import folders

    st = State.load()
    report = dfu_saves.Report()
    inst = current_install()
    if inst is None:
        report.skipped = "Daggerfall Unity isn't installed."
        return report
    if not force and (SAVES_FOLDER.key not in st.game_folders or folders.stopping(SAVES_FOLDER.key, st)):
        report.skipped = "Saves don't sync on this machine."
        return report
    (inst.path / SAVES_MARKER).touch()
    return dfu_saves.sync(saves_dir(inst), saves_library(), _machine(),
                          guard=lambda: _free_to_sync(inst, playing))


SAVES_FOLDER = FolderSpec(
    key="dfu-saves",
    label="Daggerfall Unity saves (ModSync)",
    path=_saves_root,
    ignore=(
        "// Managed by ModSync: Daggerfall Unity's saves, one folder per version, never",
        "// written twice. ModSync copies them to and from DFU's own Saves folder.",
        ".incoming-*",
    ),
    in_use=lambda: is_running(),
    tidy=lambda root: sync_saves(),
    on_stop=lambda: _release_saves(),
)


def _release_saves() -> None:
    from modsync import dfu_saves

    inst = current_install()
    if inst is not None:
        dfu_saves.release(saves_dir(inst), saves_library())


# --- for the app --------------------------------------------------------------------


@dataclass
class Overview:
    """Everything the app shows about Daggerfall Unity, read in one go (off
    the UI thread: it reads Steam's libraries)."""

    dagger: Path | None  # Steam's DF/DAGGER, if Steam installed Daggerfall
    problems: list[str]  # why DFU would reject that data; empty when it's fine
    install: Install | None
    configured: str | None  # the MyDaggerfallPath the install uses
    hook: "launchhook.LaunchHookStatus | None"
    mods: str  # "on" | "off" | "stopping"
    saves: str
    version_problem: str | None
    version: str | None = None  # the install's DFU version, read with the rest
    configured_ok: bool = False  # DFU would accept the data the install uses (Steam's or its own copy)

    @property
    def stale_path(self) -> bool:
        """Steam has Daggerfall, and the install uses neither it nor other data DFU accepts."""
        return bool(self.install and self.dagger and not self.configured_ok
                    and Path(self.configured or "") != self.dagger)

    @property
    def own_data(self) -> bool:
        """The install uses a copy of Daggerfall other than Steam's (say, GOG's), which works."""
        return bool(self.install and self.configured_ok and Path(self.configured or "") != self.dagger)


def overview(*, hook: bool = True) -> Overview:
    """``hook``: also read the Play hook's status (Steam's config files)."""
    from modsync.sync import folders

    dagger = steam_daggerfall()
    st = State.load()
    inst = current_install()

    def sync_state(spec: FolderSpec) -> str:
        if folders.stopping(spec.key, st):
            return "stopping"
        return "on" if spec.key in st.game_folders else "off"

    status = None
    if hook:
        try:
            status = hook_status()
        except Exception as exc:  # Steam's files unreadable: show the rest
            log.warning("could not read the Play hook's status: %s", exc)
    configured = inst.daggerfall_path() if inst else None
    problems = data_problems(dagger)
    configured_ok = bool(configured) and not (problems if Path(configured) == dagger else data_problems(Path(configured)))
    return Overview(dagger, problems, inst, configured, status,
                    sync_state(MODS_FOLDER), sync_state(SAVES_FOLDER),
                    version_problem(inst) if inst else None, inst.version if inst else None, configured_ok)


def setup(dest: Path | str | None = None, *, progress: engine.ProgressFn | None = None) -> tuple[Installed, str | None]:
    """Install DFU (or point an install at Steam's data again) and remember
    it; an enabled Play hook moves along. Returns the result and what the
    hook said, if anything."""
    from modsync import config

    dagger = steam_daggerfall()
    problems = data_problems(dagger)
    if problems:
        raise DfuError(" ".join(problems))
    if not dest:
        # The install it shows, if any: repairing never makes a second one elsewhere,
        # and only points it at Steam's data again, whoever installed it.
        shown = current_install()
        if shown is not None:
            configure(shown, dagger)
            State.update(dfu_path=str(shown.path))
            return Installed(shown, dagger, fresh=False), follow_install(shown)
        dest = Path(State.load().dfu_path or default_install_dir())
    result = install(Path(dest), dagger, cache_dir=config.data_dir() / "dfu", progress=progress,
                     resolution=display_resolution())
    State.update(dfu_path=str(result.install.path))
    return result, follow_install(result.install)


def launch(inst: Install) -> str:
    """Start DFU from the app. With the Play hook on, through Steam (so it is
    Steam's launch, and the hook syncs the saves around it); otherwise
    directly, its saves synced first and again once it has closed (at the
    next sync status). Returns what happened."""
    if is_running():
        return "Daggerfall Unity is already running."
    st = hook_status()
    if st.enabled and st.native == str(inst.executable):
        result = background.run_host(["xdg-open", f"steam://rungameid/{DAGGERFALL_APPID}"], timeout=15)
        if result.returncode == 0:
            return "Starting Daggerfall Unity through Steam."
        log.warning("xdg-open steam:// failed (%s); starting DFU directly", result.returncode)
    report = sync_saves()
    if report.changed:
        log.info("before playing: %s", report.summary())
    cmd = [str(inst.executable)]
    if background.in_flatpak():
        cmd = ["flatpak-spawn", "--host", f"--directory={inst.path}", *cmd]
    # As a native game would get it: not the libraries Steam preloads for ModSync, nor Qt's.
    env = {k: v for k, v in os.environ.items()
           if k not in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "QT_QPA_PLATFORM", "QT_PLUGIN_PATH")}
    subprocess.Popen(cmd, cwd=inst.path, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return "Starting Daggerfall Unity."


def open_mods_folder() -> Path:
    """The mods folder, made if it isn't there yet (for the app to show)."""
    inst = current_install()
    if inst is None:
        raise DfuError("Daggerfall Unity isn't installed.")
    inst.mods_dir.mkdir(parents=True, exist_ok=True)
    return inst.mods_dir
