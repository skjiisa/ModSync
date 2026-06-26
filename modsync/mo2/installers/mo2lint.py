"""MO2-LINT backend: drives the prebuilt ``mo2-lint`` binary as a subprocess.

MO2-LINT installs a portable MO2 instance for a game, reusing the game's existing
Steam/Proton prefix and wiring it via a Steam launch option. We download a pinned
prebuilt binary (all current releases are pre-releases, so GitHub's "latest"
endpoint skips them). Requires ``protontricks`` on the system.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import urllib.request
from pathlib import Path

from modsync.config import data_dir
from modsync.games import Game
from modsync.mo2.installers.base import InstallerBackend, InstallResult, OnOutput

MO2LINT_VERSION = "7.0.0-rc5"
MO2LINT_URL = (
    "https://github.com/Furglitch/modorganizer2-linux-installer"
    f"/releases/download/{MO2LINT_VERSION}/mo2-lint"
)


def mo2lint_path() -> Path:
    return data_dir() / "bin" / "mo2-lint"


def ensure_mo2lint(force: bool = False) -> Path:
    dest = mo2lint_path()
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(MO2LINT_URL, headers={"User-Agent": "ModSync"})
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 (trusted host)
        blob = resp.read()
    dest.write_bytes(blob)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dest


class Mo2LintBackend(InstallerBackend):
    name = "MO2-LINT"

    def __init__(self, binary: Path | str | None = None) -> None:
        self._binary = Path(binary) if binary else None

    def binary(self) -> Path:
        if self._binary is not None:
            return self._binary
        system = shutil.which("mo2-lint")
        return Path(system) if system else ensure_mo2lint()

    def available(self) -> tuple[bool, str]:
        if shutil.which("protontricks") is None:
            return False, "protontricks is not installed (required by MO2-LINT)"
        return True, ""

    def install(
        self,
        game: Game,
        dest_dir: Path | str,
        *,
        script_extender: bool = False,  # mo2-lint rc5 auto-SKSE is broken; opt-in only
        on_output: OnOutput | None = None,
    ) -> InstallResult:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        args = [
            str(self.binary()),
            "install",
            game.mo2lint_key,
            str(dest_dir),
            "--unattended",
        ]
        if script_extender:
            args.append("--script-extender")

        if on_output:
            on_output("$ " + " ".join(args))

        with subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                if on_output:
                    on_output(line.rstrip("\n"))
            returncode = proc.wait()

        has_exe = (dest_dir / "ModOrganizer.exe").exists()
        success = returncode == 0 and has_exe
        if success:
            message = ""
        elif returncode != 0:
            message = f"mo2-lint exited with code {returncode}"
        else:
            message = "mo2-lint finished but ModOrganizer.exe was not found"
        return InstallResult(success, returncode, dest_dir if success else None, message)
