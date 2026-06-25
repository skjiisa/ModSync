"""Steam library + installed-app discovery by parsing ``libraryfolders.vdf``.

Only the real ``<steam_root>/steamapps/libraryfolders.vdf`` (or the ``config/``
copy) is read — never the stray copies that live inside Proton prefixes under
``compatdata/*/pfx/drive_c/...``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from modsync.steam import vdf


@dataclass(frozen=True)
class Library:
    path: Path
    app_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def steamapps(self) -> Path:
        return self.path / "steamapps"


@dataclass(frozen=True)
class App:
    appid: int
    name: str
    installdir: str
    library: Library

    @property
    def install_path(self) -> Path:
        return self.library.steamapps / "common" / self.installdir


def _libraryfolders_vdf(steam_root: Path) -> Path | None:
    for rel in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
        p = steam_root / rel
        if p.exists():
            return p
    return None


def _canonical(path_str: str) -> Path:
    try:
        return Path(path_str).resolve()
    except OSError:
        return Path(path_str)


def read_libraries(steam_root: Path) -> list[Library]:
    vdf_path = _libraryfolders_vdf(Path(steam_root))
    if not vdf_path:
        return []
    try:
        data = vdf.load(vdf_path)
    except OSError:
        return []
    folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
    if not isinstance(folders, dict):
        return []

    libraries: list[Library] = []
    seen: set[Path] = set()
    for key, val in folders.items():
        path_str: str | None = None
        app_ids: set[int] = set()
        if isinstance(val, dict):
            if str(val.get("mounted", "1")) == "0":
                continue
            raw_path = val.get("path")
            path_str = raw_path if isinstance(raw_path, str) else None
            apps = val.get("apps")
            if isinstance(apps, dict):
                for a in apps:
                    try:
                        app_ids.add(int(a))
                    except (ValueError, TypeError):
                        pass
        elif isinstance(val, str) and key.isdigit():
            # very old flat format: "1"  "/path/to/library"
            path_str = val
        if not path_str:
            continue
        real = _canonical(path_str)
        if real in seen:
            continue
        seen.add(real)
        libraries.append(Library(real, frozenset(app_ids)))
    return libraries


def all_libraries(steam_roots: Iterable[Path]) -> list[Library]:
    out: list[Library] = []
    seen: set[Path] = set()
    for root in steam_roots:
        for lib in read_libraries(Path(root)):
            if lib.path in seen:
                continue
            seen.add(lib.path)
            out.append(lib)
    return out


def _read_manifest(acf: Path) -> tuple[int, str, str] | None:
    try:
        data = vdf.load(acf).get("AppState", {})
    except OSError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        appid = int(data.get("appid", 0))
    except (ValueError, TypeError):
        return None
    return appid, str(data.get("name", "")), str(data.get("installdir", ""))


def iter_apps(library: Library) -> Iterator[App]:
    sa = library.steamapps
    if not sa.is_dir():
        return
    for acf in sorted(sa.glob("appmanifest_*.acf")):
        m = _read_manifest(acf)
        if m:
            yield App(m[0], m[1], m[2], library)


def find_app(libraries: Iterable[Library], appid: int) -> App | None:
    for lib in libraries:
        acf = lib.steamapps / f"appmanifest_{appid}.acf"
        if acf.exists():
            m = _read_manifest(acf)
            if m:
                return App(m[0], m[1], m[2], lib)
    return None
