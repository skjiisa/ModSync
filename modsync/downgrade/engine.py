"""Run a game downgrade: download → verify → extract → xdelta3 → swap → verify.

Design notes:

* **Downloads are cached** under ModSync's data dir, keyed by SHA1 (or by URL
  when the recipe has no hash for a part), so a second downgrade after the
  next Bethesda patch is offline and instant. Interrupted downloads resume.
* **Patching is staged.** Every patched file is written to a work dir *inside
  the game folder* (same filesystem) and only when all of them succeeded are
  they moved over the originals with atomic renames. xdelta3 verifies the
  source checksum, so a wrong source version fails before anything is touched.
* **Steam keeps working.** We never edit Steam's appmanifest here; that is
  ``service.pin_game_version`` (see ``steam/appmanifest.py``). Right after a
  Steam update the manifest already claims the current build, so the game
  launches from Steam immediately after the downgrade.
"""

from __future__ import annotations

import hashlib
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
    )


def sha1_of(path: Path) -> str:
    h = hashlib.sha1()  # noqa: S324 (matching the recipe's checksums)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight(plan: Plan) -> None:
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
    return cache_dir / key / f"{archive.stem}{suffix}"


def _download(url: str, dest: Path, progress: ProgressFn | None, label: str) -> int:
    """Download with resume. Returns bytes transferred this call."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    headers = {"User-Agent": "ModSync"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=60)  # noqa: S310 (https URLs from the recipe)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:  # range not satisfiable: we already have it all
            return 0
        raise DowngradeError(f"download failed ({exc.code}) for {url}") from exc
    except urllib.error.URLError as exc:
        raise DowngradeError(f"download failed for {url}: {exc.reason}") from exc
    with resp:
        if have and resp.status != 206:
            have = 0  # server ignored the range; start over
        length = resp.headers.get("Content-Length")
        total = (int(length) + have) if length else None
        mode = "ab" if have else "wb"
        moved = 0
        with dest.open(mode) as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                moved += len(chunk)
                if progress:
                    progress(Progress("download", label, have + moved, total))
    return moved


def download_all(plan: Plan, cache_dir: Path, progress: ProgressFn | None = None) -> tuple[list[Path], int]:
    """Fetch (or reuse) every archive; returns joined single-file archives."""
    joined: list[Path] = []
    transferred = 0
    for archive in plan.archives:
        part_paths: list[Path] = []
        for i, part in enumerate(archive.parts):
            dest = _part_cache_path(cache_dir, archive, i)
            label = f"{archive.stem} part {i + 1}/{len(archive.parts)}"
            ok = dest.exists() and (part.sha1 is None or sha1_of(dest) == part.sha1)
            if not ok:
                transferred += _download(part.url, dest, progress, label)
                if part.sha1 and sha1_of(dest) != part.sha1:
                    dest.unlink(missing_ok=True)
                    raise DowngradeError(f"checksum mismatch for {part.url}; deleted the download, please retry")
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
    if work.exists():
        shutil.rmtree(work)
    extract_root = work / "patches"
    out_root = work / "out"
    notes: list[str] = []
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

        swapped: list[Path] = []
        for i, (_, rel) in enumerate(patches, 1):
            if progress:
                progress(Progress("swap", f"Installing {rel}", i, len(patches)))
            (out_root / rel).replace(plan.game_dir / rel)
            swapped.append(plan.game_dir / rel)

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
        return Result(plan.target, str(installed), swapped, 0, notes)
    finally:
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
