"""Give a portable MO2 instance a current Visual C++ runtime of its own.

MO2 2.5 is built with Visual Studio 2022, but Skyrim's Proton prefix often has
only the VS2015 runtime (14.00.24215, from 2016) that the game's own
redistributable installs into ``system32``. VS2022's ``std::mutex`` crashes on
that old ``msvcp140.dll`` (a null read inside ``_Mtx_lock``), so MO2 dies at
start-up before it writes a log, and its crash handler then writes hundreds of
dumps. Found on a Steam Frame; any prefix with only the 2016 runtime is affected.

Windows looks for a DLL next to the program first, and Proton loads these
``native,builtin``, so copying a current runtime next to ``ModOrganizer.exe``
fixes it without touching the prefix. The DLLs come from Microsoft's own
redistributable, pinned by URL and SHA-256 and unpacked here: it is a WiX bundle
whose payload cabinets are MSZIP, which ``zlib`` reads. Nothing is installed or
run. The instance root is machine-local and never synced (see ``stignore``).

Most prefixes never need this: MO2-LINT installs Microsoft's redistributable
into the prefix as part of Install MO2, and MO2 then loads that copy. The
download only happens when the prefix's runtime is missing, Wine's stand-in, or
too old (see :func:`prefix_runtime_problems`).
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import struct
import tempfile
import urllib.request
import zlib
from pathlib import Path

from modsync.config import data_dir
from modsync.steam import pe

log = logging.getLogger(__name__)

VERSION = (14, 44, 35211, 0)
REDIST_URL = (
    "https://download.visualstudio.microsoft.com/download/pr/bd1c8d9d-ba95-4eee-bc6e-df1fcc876373/"
    "CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe"
)
REDIST_SHA256 = "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b"

# What MO2 2.5 and its plugins import (msvcp140_atomic_wait by one plugin),
# plus the concurrency runtime the others can pull in.
DLLS = (
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "msvcp140_atomic_wait.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "concrt140.dll",
)

_CAB_SIGNATURE = b"MSCF\0\0\0\0"
_PE32_PLUS_AMD64 = 0x8664


def _is_amd64_dll(data: bytes) -> bool:
    if data[:2] != b"MZ" or len(data) < 0x40:
        return False
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    return data[pe:pe + 4] == b"PE\0\0" and struct.unpack_from("<H", data, pe + 4)[0] == _PE32_PLUS_AMD64


def _cab_members(cab: bytes) -> dict[str, bytes]:
    """Every file in a single-cabinet (no spanning) archive using no compression
    or MSZIP, which is all a VC++ redistributable uses."""
    if cab[:8] != _CAB_SIGNATURE:
        raise ValueError("not a cabinet")
    files_at, = struct.unpack_from("<I", cab, 16)
    folder_count, file_count, flags = struct.unpack_from("<HHH", cab, 26)
    pos = 36
    folder_reserve = data_reserve = 0
    if flags & 0x4:
        header_reserve, folder_reserve, data_reserve = struct.unpack_from("<HBB", cab, pos)
        pos += 4 + header_reserve
    if flags & 0x3:
        raise ValueError("multi-cabinet sets are not supported")

    folders: list[bytes] = []
    for i in range(folder_count):
        data_at, block_count, compression = struct.unpack_from("<IHH", cab, pos + i * (8 + folder_reserve))
        out = bytearray()
        for _ in range(block_count):
            packed_size, unpacked_size = struct.unpack_from("<HH", cab, data_at + 4)
            block = cab[data_at + 8 + data_reserve:data_at + 8 + data_reserve + packed_size]
            data_at += 8 + data_reserve + packed_size
            before = len(out)
            kind = compression & 0xF
            if kind == 0:
                out += block
            elif kind == 1:
                if block[:2] != b"CK":
                    raise ValueError("bad MSZIP block")
                # Each block is its own deflate stream primed with the last 32 KiB.
                inflate = zlib.decompressobj(-zlib.MAX_WBITS, zdict=bytes(out[-32768:]))
                out += inflate.decompress(block[2:]) + inflate.flush()
            else:
                raise ValueError(f"unsupported cabinet compression {kind}")
            if len(out) - before != unpacked_size:
                raise ValueError("cabinet block has the wrong size")
        folders.append(bytes(out))

    members: dict[str, bytes] = {}
    pos = files_at
    for _ in range(file_count):
        size, offset, folder = struct.unpack_from("<IIH", cab, pos)
        end = cab.index(b"\0", pos + 16)
        name = cab[pos + 16:end].decode("utf-8", "replace")
        pos = end + 1
        if folder < len(folders):
            members[name] = folders[folder][offset:offset + size]
    return members


def _embedded_cabs(blob: bytes) -> list[bytes]:
    cabs = []
    at = blob.find(_CAB_SIGNATURE)
    while at >= 0:
        size, = struct.unpack_from("<I", blob, at + 8)
        if 0 < size <= len(blob) - at:
            cabs.append(blob[at:at + size])
        at = blob.find(_CAB_SIGNATURE, at + 8)
    return cabs


def extract_dlls(redist: bytes) -> dict[str, bytes]:
    """The x64 runtime DLLs from a ``VC_redist.x64.exe``.

    The bundle carries its packages in cabinets appended to the exe; the x64 MSI
    packages' own cabinets name each file ``<dll>_amd64``."""
    wanted = {f"{name}_amd64": name for name in DLLS}
    found: dict[str, bytes] = {}
    for outer in _embedded_cabs(redist):
        for payload in _cab_members(outer).values():
            if payload[:8] != _CAB_SIGNATURE:
                continue
            for member, data in _cab_members(payload).items():
                if member in wanted and _is_amd64_dll(data):
                    found[wanted[member]] = data
    missing = sorted(set(DLLS) - set(found))
    if missing:
        raise RuntimeError(f"the VC++ redistributable is missing {', '.join(missing)}")
    return found


def cache_dir() -> Path:
    return data_dir() / "vcruntime" / ".".join(map(str, VERSION))


def _download() -> bytes:
    req = urllib.request.Request(REDIST_URL, headers={"User-Agent": "ModSync"})
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 (fixed https URL)
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != REDIST_SHA256:
        raise RuntimeError(f"the VC++ redistributable download did not match its checksum (got {digest})")
    return data


def ensure_cached() -> Path:
    """The cached runtime DLLs, downloading and unpacking them once."""
    dest = cache_dir()
    if all((dest / name).is_file() for name in DLLS):
        return dest
    dlls = extract_dlls(_download())
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Unpack beside the cache and rename into place, so an interrupted run never
    # leaves a partial set that the check above would trust.
    staging = Path(tempfile.mkdtemp(dir=dest.parent, prefix=".vcruntime-"))
    try:
        for name, data in dlls.items():
            (staging / name).write_bytes(data)
        if dest.exists():
            shutil.rmtree(dest)
        staging.replace(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    log.info("cached VC++ runtime %s in %s", ".".join(map(str, VERSION)), dest)
    return dest


def outdated(instance: Path | str) -> list[str]:
    """The runtime DLLs the instance lacks, or has older than :data:`VERSION`."""
    root = Path(instance)
    return [name for name in DLLS if (pe.file_version(root / name) or (0,)) < VERSION]


def ensure_instance_runtime(instance: Path | str, *, system32: Path | None = None) -> list[str]:
    """Copy a current runtime next to the instance's ``ModOrganizer.exe``.

    Returns the DLLs it copied. Newer copies already there are left alone. When
    ``system32`` (the game prefix's) is given and already holds a current
    runtime, nothing is copied or downloaded: MO2 loads the prefix's copy."""
    root = Path(instance)
    if not (root / "ModOrganizer.exe").is_file():
        return []
    stale = outdated(root)
    if system32 is not None and prefix_has_mo2_runtime(system32):
        # MO2 loads the prefix's copies, except where an old copy beside it
        # comes first in the DLL search order: those still get replaced.
        stale = [name for name in stale if (root / name).is_file()]
        if not stale:
            log.debug("the game prefix at %s has a current VC++ runtime; MO2 uses that", system32)
    if not stale:
        return []
    source = ensure_cached()
    for name in stale:
        shutil.copyfile(source / name, root / name)
    log.info("copied VC++ runtime %s into %s: %s", ".".join(map(str, VERSION)), root, ", ".join(stale))
    return stale


# --- the game prefix's runtime ---------------------------------------------------
#
# From 0.5.7, USVFS links the VC++ runtime dynamically. MO2 injects it into the
# programs it starts (the game, SKSE, tools), where it uses *that* process's
# runtime: the game folder's or the prefix's system32. A Skyrim prefix typically
# has the 2016 msvcp140.dll the game's own redistributable installed, plus Wine's
# built-in vcruntime140_1.dll, and the injected USVFS then dies on its first C++
# exception, so nothing started from MO2 runs. The copy next to ModOrganizer.exe
# can't help there. Installing Microsoft's redistributable into the prefix does
# (MO2-LINT does this too); ModSync offers it, never does it on its own.

USVFS_DYNAMIC_RUNTIME = (0, 5, 7, 0)
PREFIX_RUNTIME_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
PREFIX_MIN_VERSION = (14, 40, 0, 0)  # VS2022 17.10 (constexpr std::mutex)
# Everything vc_redist.x64.exe may replace in system32.
PREFIX_BACKUP_GLOBS = ("msvcp140*.dll", "vcruntime140*.dll", "concrt140.dll", "vcomp140.dll",
                       "vccorlib140.dll", "vcamp140.dll", "mfc140*.dll", "mfcm140*.dll")
_WINE_BUILTIN = b"Wine builtin DLL"


def usvfs_needs_prefix_runtime(instance: Path | str) -> bool:
    """Whether the instance's USVFS uses the runtime of the programs it's injected into."""
    version = pe.file_version(Path(instance) / "usvfs_x64.dll")
    return version is not None and version >= USVFS_DYNAMIC_RUNTIME


def is_wine_builtin(path: Path) -> bool:
    """Wine's own stand-in DLLs carry this marker in their DOS stub."""
    try:
        with open(path, "rb") as fh:
            return _WINE_BUILTIN in fh.read(0x400)
    except OSError:
        return False


def prefix_has_mo2_runtime(system32: Path) -> bool:
    """Whether the prefix holds every runtime DLL MO2 and its plugins import,
    current and not Wine's stand-in (the full :data:`DLLS` set, not just the
    three USVFS needs)."""
    return not _runtime_problems(system32, DLLS)


def prefix_runtime_problems(system32: Path) -> list[str]:
    """Why the prefix's runtime is too old for USVFS 0.5.7+, one line per DLL; [] if fine."""
    return _runtime_problems(system32, PREFIX_RUNTIME_DLLS)


def _runtime_problems(system32: Path, names: tuple[str, ...]) -> list[str]:
    problems = []
    for name in names:
        path = system32 / name
        if not path.is_file():
            problems.append(f"{name} is missing")
        elif is_wine_builtin(path):
            problems.append(f"{name} is Wine's built-in stand-in")
        else:
            version = pe.file_version(path)
            if version is None or version < PREFIX_MIN_VERSION:
                shown = ".".join(map(str, version)) if version else "unknown version"
                problems.append(f"{name} is {shown}")
    return problems


def backup_prefix_runtime(system32: Path, dest: Path) -> Path:
    """Copy the runtime DLLs the redistributable may replace into ``dest``."""
    dest.mkdir(parents=True, exist_ok=True)
    for pattern in PREFIX_BACKUP_GLOBS:
        for path in system32.glob(pattern):
            if path.is_file():
                shutil.copy2(path, dest / path.name)
    return dest


def redist_installer() -> Path:
    """The pinned, verified VC_redist.x64.exe, downloaded once."""
    path = cache_dir().parent / f"VC_redist.x64-{'.'.join(map(str, VERSION))}.exe"
    if not path.is_file():
        data = _download()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
    return path
