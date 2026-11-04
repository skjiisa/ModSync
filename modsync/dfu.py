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
    if resolution:
        width, height = resolution
        write_settings(inst.settings_ini, "Video", {"ResolutionWidth": width, "ResolutionHeight": height})
    log.info("installed Daggerfall Unity %s into %s (resolution %s)", DFU_VERSION, dest, resolution or "DFU's default")
    return Installed(inst, dagger, fresh=True)


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


def _install_root() -> Path | None:
    inst = find_install(State.load().dfu_path)
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
def _not_playing(inst: Install, playing: bool):
    """While Steam's Play hook has DFU starting or running it holds this lock;
    any other sync then gives way (yields a reason). The hook's own syncs,
    just before and after DFU, say ``playing``. Held shared for the sync, so
    the hook waits for one under way before starting DFU."""
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
    inst = find_install(st.dfu_path)
    if inst is None:
        report.skipped = "Daggerfall Unity isn't installed."
        return report
    if not force and (SAVES_FOLDER.key not in st.game_folders or folders.stopping(SAVES_FOLDER.key, st)):
        report.skipped = "Saves don't sync on this machine."
        return report
    (inst.path / SAVES_MARKER).touch()
    with _not_playing(inst, playing) as reason:
        if reason:
            report.skipped = reason
            return report

        def busy() -> str:  # asked again once the saves are locked: DFU may have started meanwhile
            return "Daggerfall Unity is running; its saves sync once it closes." if is_running() else ""

        return dfu_saves.sync(saves_dir(inst), saves_library(), _machine(), busy=busy)


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
)
