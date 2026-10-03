"""Install the pinned USVFS stack-alignment backport for MO2 2.5.2 on ARM64.

Only exact known files are replaced. Originals stay beside the instance in
``.modsync-usvfs/backup``. Each replacement is atomic; an interrupted batch can
be completed or restored because every file must match either known build.
The instance root and this backup are excluded from ModSync's sync allowlist.

Source and build instructions: docs/usvfs-arm64.md.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from modsync import background
from modsync.config import data_dir
from modsync.downgrade.tools import find_extractor
from modsync.steam.compattools import is_arm64

log = logging.getLogger(__name__)

PATCH_ID = "v0.5.6.1-woa.1"
SOURCE_COMMIT = "866ce70040c4fc34fd2ddb59b3567a97f20cd50e"
ARCHIVE_URL = f"https://github.com/ndabas/usvfs/releases/download/{PATCH_ID}/usvfs_{PATCH_ID}.7z"
ARCHIVE_SHA256 = "acfbdb928078686d3f5709fc57e416ebb524292174878a297f1b6cf8d3a88f8e"
ARCHIVE_SIZE = 12_822_348
ORIGINAL = {
    "usvfs_x64.dll": "e2b766f418575021b9d350f384195ce6f23173169b37222cdef3d7fe5495f8b5",
    "usvfs_x86.dll": "c89d9587c7f725927f3ce03076e85af2dfd134909d6d769455be3d21e086a478",
    "usvfs_proxy_x64.exe": "186fa5f2b60c8a1f4e7aafd323d3131111866e58bb25bde91429a1c1b4d3def4",
    "usvfs_proxy_x86.exe": "cc41543f444c0441c93b1476cb09a55192651661231a1ae71f2ca115829cbac0",
}
PATCHED = {
    "usvfs_x64.dll": "b453f2bde5c39be1cd1ace1102f71f42e18dea1103b057d8b12a3c89ff5f5ef6",
    "usvfs_x86.dll": "482133421bd95791bc3be81b09665ccce5dd2fecb1d82838e060d4ec2313271a",
    "usvfs_proxy_x64.exe": "ce91820c18c9a56b4cc199defd534d900e1ae6be17f8ec23997959b2e6300385",
    "usvfs_proxy_x86.exe": "d2aa70835cdda8cfdf07b4afd956345fc04a696e55b23bba393d6ed94302fcf5",
}


class UsvfsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Status:
    state: str
    message: str
    can_apply: bool = False
    can_restore: bool = False


def _digest(path: Path) -> str | None:
    # Replacing a symlink would silently change which instance owns this file.
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _matches(directory: Path, expected: dict[str, str]) -> bool:
    return all(_digest(directory / name) == digest for name, digest in expected.items())


def backup_dir(instance: Path) -> Path:
    return instance / ".modsync-usvfs" / "backup"


def status(instance: Path | str) -> Status:
    root = Path(instance)
    if not (root / "ModOrganizer.exe").is_file():
        return Status("missing", "Choose an installed Mod Organizer 2 instance first.")
    try:
        actual = {name: _digest(root / name) for name in ORIGINAL}
        recoverable = _matches(backup_dir(root), ORIGINAL)
    except OSError as exc:
        return Status("unreadable", f"Could not verify this instance's USVFS files or backups: {exc}")
    restored = actual == ORIGINAL
    patched = actual == PATCHED
    known = all(actual[name] in (ORIGINAL[name], PATCHED[name]) for name in ORIGINAL)
    if patched:
        return Status("patched", "The USVFS ARM64 fix is installed.", can_restore=recoverable)
    if not is_arm64() and restored:
        return Status("not-needed", "This machine does not need the USVFS ARM64 fix.")
    if restored:
        return Status("available", "This MO2 cannot start games on ARM64 until the USVFS fix is applied.", True)
    if known and recoverable:
        return Status("interrupted", "The USVFS replacement was interrupted. Apply the fix again or restore the originals.",
                      can_apply=is_arm64(), can_restore=True)
    return Status("unknown", "This USVFS build is not recognized. ModSync cannot verify its ARM64 fix and will leave it unchanged.")


def _atomic_copy(source: Path, target: Path) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        shutil.copymode(source, tmp)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_cached() -> Path:
    """Verify cached payloads on every use; fetch and extract into private staging."""
    cache = data_dir() / "usvfs" / PATCH_ID
    if _matches(cache, PATCHED):
        return cache
    extractor = find_extractor()
    if extractor is None:
        raise UsvfsError("The USVFS fix needs 7-Zip or bsdtar. The ModSync Flatpak includes 7-Zip.")
    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".usvfs-", dir=cache.parent) as tmp:
        staging = Path(tmp)
        archive = staging / "usvfs.7z"
        request = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "ModSync"})
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as out:
            while chunk := response.read(1 << 20):
                total += len(chunk)
                if total > ARCHIVE_SIZE:
                    raise UsvfsError("The USVFS archive is larger than the pinned release.")
                digest.update(chunk)
                out.write(chunk)
        if total != ARCHIVE_SIZE or digest.hexdigest() != ARCHIVE_SHA256:
            raise UsvfsError("The USVFS download did not match its pinned SHA-256. No instance files were changed.")
        extracted = staging / "extracted"
        extractor.extract(archive, extracted)
        source = extracted / "bin"
        if not _matches(source, PATCHED):
            raise UsvfsError("The USVFS archive does not contain the expected binaries.")
        cache.mkdir(exist_ok=True)
        for name in PATCHED:
            _atomic_copy(source / name, cache / name)
    return cache


def _require_closed() -> None:
    """Check the host too: MO2 launched through Steam isn't in our Launcher."""
    pattern = r"(^|[ /\\])(ModOrganizer\.exe|SkyrimSE(Launcher)?\.exe|skse64_loader\.exe|usvfs_proxy_[^ /\\]*\.exe)([[:space:]]|$)"
    try:
        result = background.run_host(["pgrep", "-u", str(os.getuid()), "-fi", pattern], timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UsvfsError("Could not check whether MO2 is running on the host. Close it and try again.") from exc
    if result.returncode == 0:
        raise UsvfsError("Close Mod Organizer 2, Skyrim, and programs started by MO2 before changing USVFS.")
    if result.returncode != 1:
        raise UsvfsError("Could not check whether MO2 is running on the host. Close it and try again.")


@contextmanager
def _locked(root: Path):
    import fcntl

    if not (root / "ModOrganizer.exe").is_file():
        raise UsvfsError("ModOrganizer.exe is missing from the chosen instance.")
    directory = root / ".modsync-usvfs"
    if directory.is_symlink() or backup_dir(root).is_symlink() or (directory / "lock").is_symlink():
        raise UsvfsError("The USVFS backup directory must not be a symbolic link.")
    directory.mkdir(exist_ok=True)
    with (directory / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UsvfsError("Another ModSync process is changing this instance's USVFS.") from None
        yield


def apply(instance: Path | str) -> str:
    if not is_arm64():
        raise UsvfsError("The USVFS workaround is only offered on ARM64 machines.")
    root = Path(instance).resolve()
    with _locked(root):
        current = status(root)
        if current.state == "patched":
            return current.message
        if not current.can_apply:
            raise UsvfsError(current.message)
        _require_closed()
        source = ensure_cached()
        # Recheck after the download: a user may have started MO2 or updated it.
        _require_closed()
        current = status(root)
        if not current.can_apply:
            raise UsvfsError("The instance changed while preparing the fix. Check its USVFS status and try again.")
        backup = backup_dir(root)
        backup.mkdir(exist_ok=True)
        for name, expected in ORIGINAL.items():
            saved = backup / name
            if saved.exists() or saved.is_symlink():
                if _digest(saved) != expected:
                    raise UsvfsError(f"USVFS backup {saved} is not the expected original. It was left unchanged.")
            else:
                if _digest(root / name) != expected:
                    raise UsvfsError("An original USVFS file is missing from the backup; cannot replace it.")
                _atomic_copy(root / name, saved)
        if not _matches(backup, ORIGINAL) or not _matches(source, PATCHED):
            raise UsvfsError("USVFS verification failed before replacement. No instance files were changed.")
        try:
            for name in PATCHED:
                _atomic_copy(source / name, root / name)
        except OSError as exc:
            # Keep the verified backup even if rollback fails. A subsequent
            # apply/restore accepts this exact mixture of old and new files.
            try:
                for name in ORIGINAL:
                    _atomic_copy(backup / name, root / name)
            except OSError:
                log.exception("USVFS rollback incomplete in %s; originals remain in %s", root, backup)
            raise UsvfsError(f"Could not replace USVFS. Originals are in {backup}; use Restore original USVFS. {exc}") from exc
    log.info("installed USVFS %s in %s; originals in %s", PATCH_ID, root, backup)
    return "Applied the USVFS ARM64 fix. You can now try launching the game through MO2."


def restore(instance: Path | str) -> str:
    root = Path(instance).resolve()
    with _locked(root):
        backup = backup_dir(root)
        if not _matches(backup, ORIGINAL):
            raise UsvfsError("No complete, verified original USVFS backup is available.")
        current = status(root)
        if current.state in ("available", "not-needed"):
            return "The original USVFS files are already in place."
        if not current.can_restore:
            raise UsvfsError("USVFS has changed since the fix was applied. Restoration would overwrite another build.")
        _require_closed()
        for name in ORIGINAL:
            _atomic_copy(backup / name, root / name)
    log.info("restored original USVFS in %s", root)
    return "Restored the original USVFS files. Games started through this MO2 will need the fix again on ARM64."
