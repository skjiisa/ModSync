"""Run a game downgrade: download → verify → extract → xdelta3 → swap → verify.

Design notes:

* **Downloads are cached** under ModSync's data dir, keyed by SHA1 (or by URL
  when the recipe has no hash for a part), so a second downgrade after the
  next Bethesda patch is offline and instant. Interrupted downloads resume.
* **Patching is staged.** Every patched file is written to a work dir *inside
  the game folder* (same filesystem) and only when all of them succeeded are
  they moved over the originals with atomic renames. xdelta3 verifies the
  source checksum, so a wrong source version fails before anything is touched.
* **The swap is transactional.** Each original is first moved into a backup
  dir, then the patched file moved into place. If any step fails the originals
  are moved back; if even that fails the backup dir is left on disk and named
  in the error. Files the target version must not have (per the recipe) are
  moved into the same backup rather than deleted, so they roll back too.
* **The downgrade is reversible.** After a successful swap the originals stay
  in ``<game_dir>/.modsync-downgrade/backup`` next to a ``manifest.json`` that
  lists what was swapped and removed (with the recipe's SHA1 for the exe and
  the original sizes). ``restore`` moves them all back and verifies them;
  ``discard_backup`` drops them once Steam has re-installed the current
  version anyway. A new downgrade refuses to run while a backup exists —
  unless the backup is *stale*: Steam has re-installed the version it came
  from (every backed-up file is in the game folder again, byte-for-byte for
  the executables), so it preserves nothing and is simply replaced.
* **Partial downloads are never mistaken for complete ones.** Data streams into
  a ``.part`` file that is only promoted once its size matches the server's
  Content-Length (and its SHA1 matches, when the recipe has one).
* **Steam keeps working.** We never edit Steam's appmanifest here; that is
  ``service.pin_game_version`` (see ``steam/appmanifest.py``). Right after a
  Steam update the manifest already claims the current build, so the game
  launches from Steam immediately after the downgrade.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modsync import gameversion
from modsync.downgrade import tools
from modsync.downgrade.recipe import Archive, Index

_CHUNK = 1 << 20
WORK_DIR_NAME = ".modsync-downgrade"
MANIFEST_NAME = "manifest.json"
NO_BACKUP_MESSAGE = (
    "no ModSync backup of the original game files was found, so there is nothing to restore. "
    "To get the current version back, use \"Verify integrity of game files\" in Steam "
    "(Library → the game → Properties → Installed Files)."
)


class DowngradeError(RuntimeError):
    pass


@dataclass
class Progress:
    stage: str  # "download" | "extract" | "patch" | "swap" | "post" | "verify" | "restore"
    message: str
    done: int | None = None
    total: int | None = None


ProgressFn = Callable[[Progress], None]


@dataclass
class Plan:
    game_dir: Path
    from_version: str
    from_exe_sha1: str
    target: str
    language: str
    archives: list[Archive]
    exe_name: str
    post_steps: list[str] = field(default_factory=list)
    estimated_bytes: int | None = None
    deletes: list[str] = field(default_factory=list)

    @property
    def urls(self) -> list[str]:
        return [p.url for a in self.archives for p in a.parts]


@dataclass
class Result:
    target: str
    installed_version: str | None
    patched_files: list[Path]
    downloaded_bytes: int
    notes: list[str] = field(default_factory=list)
    removed_files: list[Path] = field(default_factory=list)
    backup_dir: Path | None = None  # where the originals are kept for ``restore``


@dataclass
class RestoreResult:
    game_dir: Path
    restored: list[Path]  # game-relative paths moved back into place
    mismatches: list[str]  # restored files whose hash/size differs from the record
    removed: list[Path] = field(default_factory=list)  # files the downgrade added, now deleted
    from_version: str | None = None  # the version the backup came from, per the manifest
    target: str | None = None  # the version the downgrade had installed


def work_dir_for(game_dir: Path | str) -> Path:
    return Path(game_dir) / WORK_DIR_NAME


def backup_dir_for(game_dir: Path | str) -> Path:
    return work_dir_for(game_dir) / "backup"


def has_backup(game_dir: Path | str | None) -> bool:
    """Does a previous downgrade's backup of the originals exist here?"""
    if not game_dir:
        return False
    root = backup_dir_for(game_dir)
    return root.is_dir() and _has_backups(root)


def make_plan(index: Index, game_dir: Path | str, target: str, language: str) -> Plan:
    return Plan(
        game_dir=Path(game_dir),
        from_version=index.from_version,
        from_exe_sha1=index.from_exe_sha1,
        target=target,
        language=language,
        archives=index.archives_for(target, language),
        exe_name=index.exe_name,
        post_steps=index.post_steps,
        estimated_bytes=index.estimated_bytes(target),
        deletes=index.deletes_for(target),
    )


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()  # noqa: S324 (matching the recipe's checksums)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight(plan: Plan) -> None:
    _check_backups(plan.game_dir, work_dir_for(plan.game_dir))
    exe = plan.game_dir / plan.exe_name
    if not exe.is_file():
        raise DowngradeError(f"{exe} not found")
    actual = sha1_of(exe)
    if actual != plan.from_exe_sha1:
        have = gameversion.installed_version(plan.game_dir)
        raise DowngradeError(
            f"The recipe expects {plan.exe_name} from game version {plan.from_version}, "
            f"but this install is {have or 'an unknown version'}. Let Steam update the "
            "game to the current version first, then downgrade."
        )
    missing = tools.missing_tools()
    if missing:
        raise DowngradeError("Missing tools on this machine: " + ", ".join(missing))


# --- download ---------------------------------------------------------------------


def _part_cache_path(cache_dir: Path, archive: Archive, idx: int) -> Path:
    part = archive.parts[idx]
    key = part.sha1 or hashlib.sha1(part.url.encode()).hexdigest()  # noqa: S324
    suffix = f".{idx + 1:03d}" if len(archive.parts) > 1 else ""
    # Old unhashed entries may contain interrupted downloads. Hashed entries
    # remain safe to reuse because download_all verifies their content.
    root = cache_dir if part.sha1 else cache_dir / "complete-v2"
    return root / key / f"{archive.stem}{suffix}"


def _download(
    url: str, dest: Path, progress: ProgressFn | None, label: str, sha1: str | None = None,
) -> int:
    """Download with resume into ``dest.part``; promote to ``dest`` only when
    complete. Returns bytes transferred this call."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "ModSync"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)  # noqa: S310 (https URLs from the recipe)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            # A 416 alone does not prove that the partial file is complete.
            if exc.headers.get("Content-Range") == f"bytes */{have}":
                _complete_download(part, dest, sha1)
                return 0
            part.unlink(missing_ok=True)
            raise DowngradeError(f"invalid resume size for {url}; please retry") from exc
        raise DowngradeError(f"download failed ({exc.code}) for {url}") from exc
    except urllib.error.URLError as exc:
        raise DowngradeError(f"download failed for {url}: {exc.reason}") from exc
    with resp:
        status = getattr(resp, "status", 200)
        if have and status != 206:
            have = 0  # server ignored the range; start over
        length = resp.headers.get("Content-Length")
        total = (int(length) + have) if length else None
        if status == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", resp.headers.get("Content-Range", ""))
            if not match or int(match[1]) != have or int(match[2]) + 1 != int(match[3]):
                raise DowngradeError(f"invalid resume response for {url}")
            total = int(match[3])
        moved = 0
        with part.open("ab" if have else "wb") as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                moved += len(chunk)
                if progress:
                    progress(Progress("download", label, have + moved, total))
    size = part.stat().st_size
    if total is not None and size != total:
        raise DowngradeError(
            f"download of {url} stopped at {size} of {total} bytes; run again to resume"
        )
    _complete_download(part, dest, sha1)
    return moved


def _complete_download(part: Path, dest: Path, sha1: str | None) -> None:
    if sha1 and sha1_of(part) != sha1:
        part.unlink(missing_ok=True)
        raise DowngradeError(f"checksum mismatch for {dest.name}; deleted the download, please retry")
    part.replace(dest)


def download_all(plan: Plan, cache_dir: Path, progress: ProgressFn | None = None) -> tuple[list[Path], int]:
    """Fetch (or reuse) every archive; returns joined single-file archives."""
    joined: list[Path] = []
    transferred = 0
    for archive in plan.archives:
        part_paths: list[Path] = []
        for i, part in enumerate(archive.parts):
            dest = _part_cache_path(cache_dir, archive, i)
            label = f"{archive.stem} part {i + 1}/{len(archive.parts)}"
            # A file only exists at ``dest`` once _download promoted it as
            # complete, so presence alone is trustworthy for unhashed parts.
            ok = dest.exists() and (part.sha1 is None or sha1_of(dest) == part.sha1)
            if not ok:
                if dest.exists():
                    dest.unlink()  # hash mismatch on a cached file: refetch
                transferred += _download(part.url, dest, progress, label, part.sha1)
            part_paths.append(dest)
        if len(part_paths) == 1:
            joined.append(part_paths[0])
        else:
            whole = cache_dir / "joined" / f"{archive.depot}-{part_paths[0].parent.name}.7z"
            expected = sum(p.stat().st_size for p in part_paths)
            if not whole.exists() or whole.stat().st_size != expected:
                whole.parent.mkdir(parents=True, exist_ok=True)
                tmp = whole.with_suffix(".7z.part")
                with tmp.open("wb") as out:
                    for p in part_paths:
                        with p.open("rb") as inp:
                            shutil.copyfileobj(inp, out, _CHUNK)
                tmp.replace(whole)
            joined.append(whole)
    return joined, transferred


# --- apply ------------------------------------------------------------------------


def _collect_patches(extract_root: Path) -> list[tuple[Path, Path]]:
    """(patch file, game-relative target path) for every *.xdelta under root."""
    out: list[tuple[Path, Path]] = []
    for p in sorted(extract_root.rglob("*.xdelta")):
        rel = p.relative_to(extract_root)
        out.append((p, rel.with_name(rel.name[: -len(".xdelta")])))
    return out


def _collect_whole_files(extract_root: Path) -> list[tuple[Path, Path]]:
    """(file, game-relative path) for every non-patch file under root. Files
    that only exist in the target version (e.g. 1.5.97's binkw64.dll) ship whole."""
    out: list[tuple[Path, Path]] = []
    for p in sorted(extract_root.rglob("*")):
        if p.is_file() and not p.is_symlink() and p.suffix != ".xdelta":
            out.append((p, p.relative_to(extract_root)))
    return out


def _replace(src: Path, dst: Path) -> None:
    """Atomic same-filesystem move (split out so tests can inject failures)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)


class SwapError(DowngradeError):
    """The swap failed *and* could not be rolled back; ``backup_dir`` holds the
    originals so the install can be repaired by hand or by Steam's verify."""

    def __init__(self, message: str, backup_dir: Path) -> None:
        super().__init__(message)
        self.backup_dir = backup_dir


def _swap(
    plan: Plan,
    patched: list[Path],
    backup_root: Path,
    out_root: Path,
    progress: ProgressFn | None,
) -> tuple[list[Path], list[Path]]:
    """Move originals into ``backup_root`` and patched files into place; also
    move recipe-listed deletions into the backup. Files with no original (new
    in the target version) are simply added, and deleted on rollback. Rolls
    everything back if any step fails. Returns (installed paths, removed paths)
    in the game dir."""
    game = plan.game_dir
    done: list[Path] = []
    added: list[Path] = []
    swapped: list[Path] = []
    removed: list[Path] = []
    total = len(patched) + len(plan.deletes)
    for name in plan.deletes:
        rel = Path(name)
        target = game / rel
        if (rel.is_absolute() or ".." in rel.parts or not rel.parts
                or not target.resolve().is_relative_to(game.resolve()) or rel in patched):
            raise DowngradeError(f"unsafe or conflicting recipe deletion: {name}")
    try:
        for i, rel in enumerate(patched, 1):
            if progress:
                progress(Progress("swap", f"Installing {rel}", i, total))
            target = game / rel
            if target.exists() or target.is_symlink():
                _replace(target, backup_root / rel)
                done.append(target)
            else:
                added.append(target)
            _replace(out_root / rel, target)
            swapped.append(target)
        for j, rel_s in enumerate(plan.deletes, len(patched) + 1):
            rel = Path(rel_s)
            target = game / rel
            if not target.is_file():
                continue
            if progress:
                progress(Progress("swap", f"Removing {rel}", j, total))
            _replace(target, backup_root / rel)
            done.append(target)
            removed.append(target)
    except BaseException as exc:
        failures: list[str] = []
        for target in added:
            try:
                target.unlink(missing_ok=True)
            except Exception as rexc:
                failures.append(f"{target.relative_to(game)}: {rexc}")
        for target in reversed(done):
            rel = target.relative_to(game)
            try:
                _replace(backup_root / rel, target)
            except Exception as rexc:
                failures.append(f"{rel}: {rexc}")
        if failures:
            raise SwapError(
                f"installing patched files failed ({exc}) and rolling back also failed for "
                f"{len(failures)} file(s); originals are kept in {backup_root}: " + "; ".join(failures),
                backup_root,
            ) from exc
        if not isinstance(exc, Exception):
            raise
        raise DowngradeError(f"installing patched files failed and was rolled back: {exc}") from exc
    return swapped, removed


class BackupPresentError(DowngradeError):
    """A previous downgrade's originals are still backed up and differ from
    what is installed, so a new downgrade would overwrite the only copy of them.
    The caller decides: restore them first, or discard the backup."""

    def __init__(self, backup_dir: Path) -> None:
        super().__init__(
            "the original game files from a previous downgrade are still backed up in "
            f"{backup_dir} and differ from what is installed now. Restore them first, or "
            "discard the backup if you no longer need those files."
        )
        self.backup_dir = backup_dir


def _has_backups(root: Path) -> bool:
    return any(p.is_file() or p.is_symlink() for p in root.rglob("*"))


_HASHED_SUFFIXES = {".exe", ".dll"}


def backup_is_stale(game_dir: Path | str, work_dir: Path | None = None) -> bool:
    """Does the game folder already hold everything the backup does?

    True after Steam re-installs the version a downgrade was taken from
    (typically "Verify integrity of game files"): every backed-up file exists in
    the game folder again with the same size — byte-for-byte for the small
    executables — so the backup preserves nothing and can be replaced."""
    game = Path(game_dir)
    backup = (work_dir or work_dir_for(game)) / "backup"
    if not backup.is_dir():
        return False
    files = [p for p in backup.rglob("*") if p.is_file() or p.is_symlink()]
    if not files:
        return False
    for src in files:
        target = game / src.relative_to(backup)
        try:
            if src.is_symlink() or target.is_symlink() or not target.is_file():
                return False
            if src.stat().st_size != target.stat().st_size:
                return False
            if src.suffix.lower() in _HASHED_SUFFIXES and sha1_of(src) != sha1_of(target):
                return False
        except OSError:
            return False
    return True


def _check_backups(game_dir: Path, work: Path) -> None:
    """Refuse while a backup that still matters exists; stale ones are fine."""
    backup = work / "backup"
    if backup.is_dir() and _has_backups(backup) and not backup_is_stale(game_dir, work):
        raise BackupPresentError(backup)


def _write_manifest(work: Path, plan: Plan, patched: list[Path]) -> None:
    """Record what the swap is about to do so ``restore`` can verify its work.
    The recipe only knows the exe's hash; sizes cover the rest."""
    originals: dict[str, dict] = {}
    added: list[str] = []
    for rel in patched:
        entry: dict = {}
        target = plan.game_dir / rel
        if not (target.exists() or target.is_symlink()):
            added.append(rel.as_posix())
            continue
        try:
            entry["size"] = target.stat().st_size
        except OSError:
            pass
        if rel == Path(plan.exe_name):
            entry["sha1"] = plan.from_exe_sha1
        originals[rel.as_posix()] = entry
    removed: dict[str, dict] = {}
    for name in plan.deletes:
        target = plan.game_dir / name
        if target.is_file():
            removed[Path(name).as_posix()] = {"size": target.stat().st_size}
    doc = {
        "schema": 1,
        "from_version": plan.from_version,
        "target": plan.target,
        "language": plan.language,
        "exe_name": plan.exe_name,
        "originals": originals,
        "removed": removed,
        "added": added,
    }
    work.mkdir(parents=True, exist_ok=True)
    (work / MANIFEST_NAME).write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _read_manifest(work: Path) -> dict:
    try:
        doc = json.loads((work / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def apply(
    plan: Plan,
    archives: list[Path],
    *,
    prefix_dir: Path | None = None,
    progress: ProgressFn | None = None,
    work_dir: Path | None = None,
) -> Result:
    xdelta3 = tools.find_xdelta3()
    extractor = tools.find_extractor()
    if xdelta3 is None or extractor is None:
        raise DowngradeError("Missing tools: " + ", ".join(tools.missing_tools()))

    work = work_dir or work_dir_for(plan.game_dir)
    _check_backups(plan.game_dir, work)
    notes: list[str] = []
    if (work / "backup").is_dir() and _has_backups(work / "backup"):
        # Only a stale backup gets past _check_backups: the game folder already
        # holds those files, so replacing it loses nothing.
        notes.append("Replaced the leftover backup from an earlier downgrade (the game folder already held those files).")
    if work.exists():
        shutil.rmtree(work)
    extract_root = work / "patches"
    out_root = work / "out"
    backup_root = work / "backup"
    committed = False
    try:
        for i, archive in enumerate(archives, 1):
            if progress:
                progress(Progress("extract", f"Extracting {archive.name}", i, len(archives)))
            extractor.extract(archive, extract_root)

        patches = _collect_patches(extract_root)
        whole = _collect_whole_files(extract_root)
        if not patches and not whole:
            raise DowngradeError("the patch archives contained no files to install")
        missing = [rel for _, rel in patches if not (plan.game_dir / rel).is_file()]
        if missing:
            raise DowngradeError(
                "these game files are missing, so the patches cannot apply: "
                + ", ".join(str(m) for m in missing[:5])
            )
        need = sum((plan.game_dir / rel).stat().st_size for _, rel in patches)
        need += sum(src.stat().st_size for src, _ in whole)
        free = shutil.disk_usage(plan.game_dir).free
        if free < need * 1.05:
            raise DowngradeError(
                f"not enough free space: patching needs about {need / 1e9:.1f} GB "
                f"of scratch space on this drive, {free / 1e9:.1f} GB free"
            )

        for i, (patch, rel) in enumerate(patches, 1):
            if progress:
                progress(Progress("patch", f"Patching {rel}", i, len(patches)))
            tools.xdelta3_apply(xdelta3, plan.game_dir / rel, patch, out_root / rel)
            patch.unlink(missing_ok=True)  # free space as we go
        for i, (src, rel) in enumerate(whole, 1):
            if progress:
                progress(Progress("patch", f"Adding {rel}", i, len(whole)))
            _replace(src, out_root / rel)
        installing = [rel for _, rel in patches] + [rel for _, rel in whole]

        staged_version = gameversion.installed_version(out_root)
        if staged_version is None or str(staged_version) != plan.target:
            raise DowngradeError(f"staged game reports version {staged_version}, expected {plan.target}")
        _write_manifest(work, plan, installing)
        swapped, removed = _swap(plan, installing, backup_root, out_root, progress)

        for step in plan.post_steps:
            if progress:
                progress(Progress("post", step))
            note = _post_step(step, plan.game_dir, prefix_dir)
            if note:
                notes.append(note)

        if progress:
            progress(Progress("verify", "Checking the installed version"))
        installed = gameversion.installed_version(plan.game_dir)
        if installed is None or str(installed) != plan.target:
            raise DowngradeError(
                f"patching finished but the game reports version {installed}, expected {plan.target}"
            )
        if removed:
            notes.append(
                "Removed files that do not belong to this version: "
                + ", ".join(str(p.relative_to(plan.game_dir)) for p in removed)
            )
        committed = True
        return Result(plan.target, str(installed), swapped, 0, notes, removed, backup_root)
    except Exception as exc:
        if _has_backups(backup_root) and not isinstance(exc, SwapError):
            raise DowngradeError(
                f"{exc}; originals are preserved in {backup_root} ('modsync game restore' puts them back)"
            ) from exc
        raise
    finally:
        # Preserve recovery files on every exceptional exit, including interrupts,
        # and keep the originals after success so the downgrade can be undone.
        if not _has_backups(backup_root):
            shutil.rmtree(work, ignore_errors=True)
        elif committed:
            for scratch in (extract_root, out_root):
                shutil.rmtree(scratch, ignore_errors=True)


# --- restore ----------------------------------------------------------------------


def restore(game_dir: Path | str, progress: ProgressFn | None = None) -> RestoreResult:
    """Move every backed-up original back to its place in the game folder and
    remove the backup. Verifies what the manifest lets us verify (the exe's
    SHA1, everyone else's size) and reports mismatches without aborting."""
    game = Path(game_dir)
    work = work_dir_for(game)
    backup = backup_dir_for(game)
    files = sorted(p for p in backup.rglob("*") if p.is_file() or p.is_symlink()) if backup.is_dir() else []
    if not files:
        raise DowngradeError(NO_BACKUP_MESSAGE)
    manifest = _read_manifest(work)
    known: dict[str, dict] = {}
    for key in ("originals", "removed"):
        block = manifest.get(key)
        if isinstance(block, dict):
            known.update({k: v for k, v in block.items() if isinstance(v, dict)})

    restored: list[Path] = []
    mismatches: list[str] = []
    for i, src in enumerate(files, 1):
        rel = src.relative_to(backup)
        if progress:
            progress(Progress("restore", f"Restoring {rel}", i, len(files)))
        target = game / rel
        _replace(src, target)
        restored.append(rel)
        expect = known.get(rel.as_posix())
        if not expect or target.is_symlink():
            continue
        if expect.get("sha1"):
            actual = sha1_of(target)
            if actual != str(expect["sha1"]).lower():
                mismatches.append(f"{rel}: SHA1 {actual} differs from the original's {expect['sha1']}")
        elif expect.get("size") is not None and target.stat().st_size != int(expect["size"]):
            mismatches.append(
                f"{rel}: {target.stat().st_size} bytes, the original had {expect['size']}"
            )
    deleted: list[Path] = []
    added = manifest.get("added")
    for name in added if isinstance(added, list) else []:
        rel = Path(str(name))
        target = game / rel
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            continue
        if target.is_file() and not target.is_symlink():
            if progress:
                progress(Progress("restore", f"Removing {rel}"))
            target.unlink()
            deleted.append(rel)
    # Everything left in the work dir is ModSync's own scratch; the backup is now empty.
    shutil.rmtree(work, ignore_errors=True)
    return RestoreResult(
        game,
        restored,
        mismatches,
        removed=deleted,
        from_version=str(manifest["from_version"]) if manifest.get("from_version") else None,
        target=str(manifest["target"]) if manifest.get("target") else None,
    )


def backup_size(game_dir: Path | str | None) -> int:
    """Bytes held by a leftover backup (0 when there is none)."""
    if not game_dir:
        return 0
    root = backup_dir_for(game_dir)
    if not root.is_dir():
        return 0
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def discard_backup(game_dir: Path | str) -> int:
    """Delete a leftover backup without restoring it (Steam has re-installed
    the current version, so the backup is stale). Returns the bytes freed."""
    game = Path(game_dir)
    work = work_dir_for(game)
    if not has_backup(game):
        raise DowngradeError(NO_BACKUP_MESSAGE)
    freed = sum(p.stat().st_size for p in work.rglob("*") if p.is_file())
    shutil.rmtree(work)
    return freed


def _post_step(step: str, game_dir: Path, prefix_dir: Path | None) -> str | None:
    if step == "remove_shader_cache":
        cache = game_dir / "Data" / "ShaderCache"
        if cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
            return "Removed Data/ShaderCache so the game rebuilds shaders for this version."
        return None
    if step == "reset_content_catalog":
        # 1.7.99 changed the format of the Creations catalogue; an old game
        # crashes reading the new one, so make the game regenerate it.
        if prefix_dir is None:
            return None
        cat = prefix_dir / "drive_c/users/steamuser/AppData/Local/Skyrim Special Edition/ContentCatalog.txt"
        try:
            if cat.is_file() and "AchievementSafe" in cat.read_text(encoding="utf-8", errors="replace"):
                cat.replace(cat.with_suffix(".bak"))
                return "Renamed the newer-format ContentCatalog.txt to ContentCatalog.bak (the game recreates it)."
        except OSError:
            return None
        return None
    return f"Unknown post step '{step}' skipped."


def run(
    plan: Plan,
    *,
    cache_dir: Path,
    prefix_dir: Path | None = None,
    progress: ProgressFn | None = None,
) -> Result:
    preflight(plan)
    archives, transferred = download_all(plan, cache_dir, progress)
    try:
        result = apply(plan, archives, prefix_dir=prefix_dir, progress=progress)
    finally:
        # The split parts are the cache; the joined copies are cheap to rebuild.
        shutil.rmtree(cache_dir / "joined", ignore_errors=True)
    result.downloaded_bytes = transferred
    return result
