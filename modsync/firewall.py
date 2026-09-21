"""Desktop firewalls vs. LAN pairing.

A desktop running ``ufw`` or ``firewalld`` with the usual deny-inbound default
silently drops everything pairing and syncing need to receive: the pairing
announcements and PIN handshake (21029), Syncthing transfers (22000/tcp) and
its local discovery (21027/udp). Nothing tells the user why "Scan network"
comes back empty. The Steam Deck ships without a firewall, so this only ever
bites the PC side.

:func:`detect` asks the host's systemd (unprivileged) whether one of those
firewalls is running; :func:`allow` opens the ports through ``pkexec``, which
puts up the desktop's normal password prompt. Both hop out of the Flatpak
sandbox with ``flatpak-spawn --host``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from modsync.background import run_host

# (port, protocol, what it's for) — keep in step with the README's table.
PORTS: tuple[tuple[int, str, str], ...] = (
    (21029, "tcp", "ModSync pairing"),
    (21029, "udp", "ModSync pairing"),
    (22000, "tcp", "Syncthing"),
    (21027, "udp", "Syncthing discovery"),
)

_KINDS = ("ufw", "firewalld")


class FirewallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Firewall:
    kind: str  # "ufw" | "firewalld"

    def allow_commands(self) -> list[list[str]]:
        """The root-level commands that open ModSync's ports, one per rule."""
        if self.kind == "ufw":
            return [
                ["ufw", "allow", f"{port}/{proto}", "comment", purpose]
                for port, proto, purpose in PORTS
            ]
        cmds = [
            ["firewall-cmd", "--permanent", f"--add-port={port}/{proto}"] for port, proto, _ in PORTS
        ]
        cmds.append(["firewall-cmd", "--reload"])
        return cmds

    def allow_script(self) -> str:
        """The same rules as one ``sh -e`` script, for a single password prompt."""
        return " && ".join(shlex.join(c) for c in self.allow_commands())


def _service_active(unit: str) -> bool:
    try:
        return run_host(["systemctl", "is-active", "--quiet", unit]).returncode == 0
    except OSError:  # no systemctl / no flatpak-spawn
        return False


def detect() -> Firewall | None:
    """The firewall running on this machine, if it's one we know how to open."""
    for kind in _KINDS:
        if _service_active(kind):
            return Firewall(kind)
    return None


def allow(fw: Firewall) -> None:
    """Open ModSync's ports in ``fw`` via ``pkexec`` (graphical password prompt).

    Raises :class:`FirewallError` if the user cancelled or the command failed.
    """
    try:
        result = run_host(["pkexec", "sh", "-c", fw.allow_script()])
    except OSError as exc:
        raise FirewallError(f"could not run pkexec: {exc}") from exc
    if result.returncode == 126 or result.returncode == 127:  # polkit: dismissed / not authorised
        raise FirewallError("cancelled")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise FirewallError(detail[-1] if detail else f"{fw.kind} exited with {result.returncode}")


def manual_instructions(fw: Firewall | None = None) -> str:
    """What to type by hand, for the README-style hint."""
    fw = fw or Firewall("ufw")
    return "sudo " + fw.allow_script().replace(" && ", " && sudo ")
