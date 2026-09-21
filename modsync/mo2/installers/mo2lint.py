"""MO2-LINT backend: drives the prebuilt ``mo2-lint`` binary as a subprocess.

MO2-LINT installs a portable MO2 instance for a game, reusing the game's existing
Steam/Proton prefix and wiring it via a Steam launch option. We download a pinned
prebuilt binary (all current releases are pre-releases, so GitHub's "latest"
endpoint skips them).

It needs Steam, Proton and the host's ``xdg-mime`` / ``pgrep``; protontricks is
bundled inside the binary and winetricks is fetched on demand. Inside the Flatpak
the binary runs on the host through ``flatpak-spawn --host`` — the sandbox can't
see Steam or Proton, and the host process gets the host's own environment, so
MO2-LINT's ``~/.config/mo2-lint`` lands where its Steam-side redirector expects.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from modsync import background
from modsync.config import data_dir
from modsync.games import Game

log = logging.getLogger(__name__)
from modsync.mo2.installers.base import InstallerBackend, InstallResult, OnOutput

MO2LINT_VERSION = "7.0.0-rc7"
MO2LINT_URL = (
    "https://github.com/Furglitch/modorganizer2-linux-installer"
    f"/releases/download/{MO2LINT_VERSION}/mo2-lint"
)


def mo2lint_path() -> Path:
    return data_dir() / "bin" / "mo2-lint"


def mo2lint_state_path() -> Path:
    """MO2-LINT's registry of the instances it installed. Hard-coded to the
    host's ~/.config by MO2-LINT and its Steam-side redirector alike, so the
    same path from inside the Flatpak (home is shared)."""
    return Path.home() / ".config" / "mo2-lint" / "state.json"


def registered_instance(directory: Path) -> dict | None:
    """The registry entry for ``directory``, if MO2-LINT thinks it manages one
    there. It refuses to install over such an entry even when the folder is
    long gone — which is exactly what a ModSync "Reset setup" + reinstall
    looks like."""
    try:
        data = json.loads(mo2lint_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    wanted = str(directory)
    for inst in data.get("instances") or []:
        if isinstance(inst, dict) and inst.get("instance_path") == wanted:
            return inst
    return None


def ensure_mo2lint(force: bool = False) -> Path:
    dest = mo2lint_path()
    if dest.exists() and not force:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(MO2LINT_URL, headers={"User-Agent": "ModSync"})
    # Download to a temp file and rename into place so an interrupted download
    # never leaves a truncated binary that `dest.exists()` would then trust.
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=".mo2-lint.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310 (trusted host)
            shutil.copyfileobj(resp, out)
        tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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

    # Host tools MO2-LINT shells out to (procps, xdg-utils). Checked on the
    # host, not in the sandbox, since that's where the install runs.
    HOST_TOOLS = ("pgrep", "xdg-mime")

    def available(self) -> tuple[bool, str]:
        try:
            probe = background.run_host(["sh", "-c", " ".join(f"command -v {t} >/dev/null || echo {t};" for t in self.HOST_TOOLS)])
        except OSError as exc:
            return False, f"cannot run commands on this system: {exc}"
        if probe.returncode != 0:
            where = "the host system from the Flatpak" if background.in_flatpak() else "this system"
            return False, f"cannot reach {where}: {(probe.stderr or probe.stdout).strip() or probe.returncode}"
        missing = probe.stdout.split()
        if missing:
            return False, (
                f"{', '.join(missing)} not installed (MO2-LINT needs procps and xdg-utils)"
            )
        return True, ""

    def install(
        self,
        game: Game,
        dest_dir: Path | str,
        *,
        script_extender: bool = False,  # see InstallerBackend.install
        on_output: OnOutput | None = None,
    ) -> InstallResult:
        dest_dir = Path(dest_dir)  # MO2-LINT creates it; pre-creating it left an empty folder on failure

        # MO2-LINT refuses a directory that's in its registry. If the instance
        # is really there, installing is the wrong move; if only the registry
        # entry survived (folder deleted, ModSync reset), have MO2-LINT forget
        # it — its own uninstall also drops the Steam launch option and
        # redirector that pointed at the old instance.
        stale = registered_instance(dest_dir)
        if stale is not None:
            if (dest_dir / "ModOrganizer.exe").exists():
                message = (
                    f"MO2-LINT already manages an MO2 instance at {dest_dir}. Choose “Use” on it "
                    "instead of installing, or pick a different folder."
                )
                log.error(message)
                return InstallResult(False, 1, None, message)
            log.info("MO2-LINT still lists %s but it's gone; clearing the stale entry", dest_dir)
            if on_output:
                on_output(f"MO2-LINT still lists an instance at {dest_dir} that no longer exists; clearing it.")
            rc = self._run(
                [str(self.binary()), "uninstall", "--directory", str(dest_dir), "--unattended"], on_output
            )
            if rc != 0 or registered_instance(dest_dir) is not None:
                message = f"mo2-lint could not clear its stale entry for {dest_dir} (exit {rc})"
                log.error(message)
                return InstallResult(False, rc, None, message)

        args = [
            str(self.binary()),
            "install",
            game.mo2lint_key,
            str(dest_dir),
            "--unattended",
        ]
        if script_extender:
            args.append("--script-extender")
        log.info("installing MO2 with MO2-LINT: %s", " ".join(args))
        returncode = self._run(args, on_output)

        has_exe = (dest_dir / "ModOrganizer.exe").exists()
        success = returncode == 0 and has_exe
        if success:
            message = ""
        elif returncode != 0:
            message = f"mo2-lint exited with code {returncode}"
        else:
            message = "mo2-lint finished but ModOrganizer.exe was not found"
        if success:
            log.info("MO2 installed to %s", dest_dir)
        else:
            log.error("MO2 install into %s failed: %s", dest_dir, message)
        return InstallResult(success, returncode, dest_dir if success else None, message)

    @staticmethod
    def _run(args: list[str], on_output: OnOutput | None) -> int:
        """Run mo2-lint (on the host when sandboxed), streaming its output."""
        if background.in_flatpak():
            args = ["flatpak-spawn", "--host", *args]
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
            return proc.wait()
