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


class DowngradeError(RuntimeError):
    pass


@dataclass
class Progress:
    stage: str  # "download" | "extract" | "patch" | "swap" | "post" | "verify"
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
    _check_backups(plan.game_dir / ".modsync-downgrade" / "backup")
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
    move recipe-listed deletions into the backup. Rolls everything back if any
    step fails. Returns (patched paths, removed paths) in the game dir."""
    game = plan.game_dir
    done: list[Path] = []
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
            _replace(target, backup_root / rel)
            done.append(target)
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


def _has_backups(root: Path) -> bool:
    return any(p.is_file() or p.is_symlink() for p in root.rglob("*"))


def _check_backups(root: Path) -> None:
    if _has_backups(root):
        raise DowngradeError(
            f"a previous downgrade left original files in {root}; restore them relative to the game "
            "folder and remove the backup directory before trying again. Alternatively, use Steam's "
            "'Verify integrity of game files', then remove the backup directory."
        )


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

    work = work_dir or (plan.game_dir / ".modsync-downgrade")
    _check_backups(work / "backup")
    if work.exists():
        shutil.rmtree(work)
    extract_root = work / "patches"
    out_root = work / "out"
    backup_root = work / "backup"
    notes: list[str] = []
    committed = False
    try:
        for i, archive in enumerate(archives, 1):
            if progress:
                progress(Progress("extract", f"Extracting {archive.name}", i, len(archives)))
            extractor.extract(archive, extract_root)

        patches = _collect_patches(extract_root)
        if not patches:
            raise DowngradeError("the patch archives contained no .xdelta files")
        missing = [rel for _, rel in patches if not (plan.game_dir / rel).is_file()]
        if missing:
            raise DowngradeError(
                "these game files are missing, so the patches cannot apply: "
                + ", ".join(str(m) for m in missing[:5])
            )
        need = sum((plan.game_dir / rel).stat().st_size for _, rel in patches)
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

        staged_version = gameversion.installed_version(out_root)
        if staged_version is None or str(staged_version) != plan.target:
            raise DowngradeError(f"staged game reports version {staged_version}, expected {plan.target}")
        swapped, removed = _swap(plan, [rel for _, rel in patches], backup_root, out_root, progress)

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
        return Result(plan.target, str(installed), swapped, 0, notes, removed)
    except Exception as exc:
        if _has_backups(backup_root) and not isinstance(exc, SwapError):
            raise DowngradeError(f"{exc}; originals are preserved in {backup_root}") from exc
        raise
    finally:
        # Preserve recovery files on every exceptional exit, including interrupts.
        if committed or not _has_backups(backup_root):
            shutil.rmtree(work, ignore_errors=True)


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
