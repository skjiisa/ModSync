"""Acquire a static Syncthing binary into ModSync's own data dir.

We bundle/manage our own copy (under ``data_dir()/bin``) rather than depend on a
system Syncthing, so it survives SteamOS updates and never collides with a user's
own Syncthing install. Linux/amd64 (and arm64) for now; other platforms later.
"""

from __future__ import annotations

import json
import platform as _platform
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from modsync.config import data_dir

GITHUB_LATEST = "https://api.github.com/repos/syncthing/syncthing/releases/latest"
_USER_AGENT = "ModSync (+https://github.com/)"


def _arch() -> str:
    m = _platform.machine().lower()
    return {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(m, m)


def syncthing_dir() -> Path:
    return data_dir() / "bin"


def syncthing_path() -> Path:
    return syncthing_dir() / "syncthing"


def is_present() -> bool:
    return syncthing_path().exists()


def _http_get(url: str, accept: str | None = None, timeout: float = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return resp.read()


def _pick_asset(release: dict, os_name: str, arch: str) -> tuple[str, str] | None:
    needle = f"syncthing-{os_name}-{arch}-"
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.startswith(needle) and name.endswith(".tar.gz"):
            return name, asset["browser_download_url"]
    return None


def ensure_syncthing(force: bool = False) -> Path:
    """Return the path to a usable syncthing binary, downloading it if needed."""
    dest = syncthing_path()
    if dest.exists() and not force:
        return dest

    os_name = "linux"
    arch = _arch()
    release = json.loads(_http_get(GITHUB_LATEST, accept="application/vnd.github+json"))
    picked = _pick_asset(release, os_name, arch)
    if not picked:
        tag = release.get("tag_name", "?")
        raise RuntimeError(
            f"No Syncthing asset for {os_name}-{arch} in release {tag}"
        )
    name, url = picked

    syncthing_dir().mkdir(parents=True, exist_ok=True)
    blob = _http_get(url)
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / name
        tarball.write_bytes(blob)
        with tarfile.open(tarball) as tf:
            candidates = [
                m
                for m in tf.getmembers()
                if m.isfile() and Path(m.name).name == "syncthing"
            ]
            if not candidates:
                raise RuntimeError("syncthing binary not found in downloaded tarball")
            # The tarball also ships small text files named 'syncthing' (e.g. the
            # UFW firewall profile under etc/); the real binary is by far the largest.
            member = max(candidates, key=lambda m: m.size)
            member.name = "syncthing"  # flatten path before extracting
            tf.extract(member, path=syncthing_dir(), filter="data")

    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest
