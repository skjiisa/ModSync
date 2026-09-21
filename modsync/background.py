"""Optional background service via systemd --user.

By default ModSync only works while the app (or ``modsync serve``) is running.
Installing this service runs ``modsync serve`` under systemd --user so that,
after you close the app, a vault keeps syncing and a queued Steam pin is applied
the moment Steam exits; ``--linger`` additionally lets it run while you're logged
out. The unit keeps its historical name so upgrades replace it in place.

Works natively and inside the Flatpak — there it hops to the host's systemd via
``flatpak-spawn --host``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "modsync-sync.service"


def in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or Path("/.flatpak-info").exists()


def _exec_start() -> str:
    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id or Path("/.flatpak-info").exists():
        return f"flatpak run {flatpak_id or 'io.github.skjiisa.ModSync'} serve"
    # Native: current interpreter + module form (works inside a venv too).
    return f"{sys.executable} -m modsync serve"


def unit_text() -> str:
    return f"""\
[Unit]
Description=ModSync background service (vault sync, Steam pin, update watch)
Documentation=https://github.com/skjiisa/ModSync

[Service]
Type=simple
ExecStart={_exec_start()}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def unit_path() -> Path:
    # Use the host ~/.config even inside Flatpak: the redirected XDG_CONFIG_HOME
    # (~/.var/app/...) wouldn't be seen by the host systemd that runs the unit.
    if in_flatpak():
        config_home = Path.home() / ".config"
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config_home / "systemd" / "user" / UNIT_NAME


def run_host(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command on the host system (hopping out of the Flatpak sandbox if
    we're in one). Output is captured unless the caller says otherwise."""
    if in_flatpak():
        cmd = ["flatpak-spawn", "--host", *cmd]
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    return subprocess.run(cmd, **kwargs)


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return run_host(["systemctl", "--user", *args])


def _checked_systemctl(*args: str) -> None:
    result = _systemctl(*args)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Background service: {' '.join(args)} failed: {detail}")


def install(enable_linger: bool = False) -> str:
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(), encoding="utf-8")
    _checked_systemctl("daemon-reload")
    _checked_systemctl("enable", "--now", UNIT_NAME)
    _checked_systemctl("is-active", UNIT_NAME)

    lines = [f"Installed {path}", "Enabled and started modsync-sync.service."]

    if enable_linger:
        linger = run_host(["loginctl", "enable-linger"])
        if linger.returncode == 0:
            lines.append("Enabled linger — sync runs even while logged out.")
        else:
            lines.append("Could not enable linger automatically; run: loginctl enable-linger")
    else:
        lines.append(
            "The service runs while you're logged in. To keep it running while "
            "logged out, re-run with --linger (or: loginctl enable-linger)."
        )
    return "\n".join(lines)


def uninstall() -> None:
    _checked_systemctl("disable", "--now", UNIT_NAME)
    path = unit_path()
    if path.exists():
        path.unlink()
    _checked_systemctl("daemon-reload")


def status() -> dict:
    path = unit_path()
    return {
        "installed": path.exists(),
        "active": _systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown",
        "enabled": _systemctl("is-enabled", UNIT_NAME).stdout.strip() or "unknown",
        "unit_path": str(path),
    }
