"""Desktop firewalls vs. LAN pairing.

A desktop running ``ufw`` or ``firewalld`` with the usual deny-inbound default
silently drops everything pairing and syncing need to receive: the pairing
announcements and PIN handshake (21029), Syncthing transfers (22000/tcp) and
its local discovery (21027/udp). Nothing tells the user why "Scan network"
comes back empty. The Steam Deck ships without a firewall, so this only ever
bites the PC side.

:func:`check` runs at launch: it asks the host's systemd (unprivileged) whether
one of those firewalls is running and then reads its rules — ``firewall-cmd
--query-port`` for firewalld, ``/etc/ufw/user.rules`` for ufw. Where the rules
can't be read without root (Debian ships that file 0640) it falls back to a
fingerprint: the rules file's mtime at the moment ModSync added its rules,
which ``stat`` exposes regardless — any later change to the rules brings the
warning back. :func:`allow` opens the ports through ``pkexec``, the desktop's
normal password prompt. Every host command hops out of the Flatpak sandbox
with ``flatpak-spawn --host``.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from modsync.background import run_host

UFW_RULES = "/etc/ufw/user.rules"

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

    def remove_commands(self) -> list[list[str]]:
        """Undo :meth:`allow_commands` — exactly the rules ModSync adds, nothing
        the user added by hand (a bare ``ufw allow 21029`` is a different rule)."""
        if self.kind == "ufw":
            return [
                ["ufw", "delete", "allow", f"{port}/{proto}", "comment", purpose]
                for port, proto, purpose in PORTS
            ]
        cmds = [
            ["firewall-cmd", "--permanent", f"--remove-port={port}/{proto}"] for port, proto, _ in PORTS
        ]
        cmds.append(["firewall-cmd", "--reload"])
        return cmds

    def remove_script(self) -> str:
        """One script that keeps going past rules that are already gone
        (``ufw delete`` on a missing rule is an error); the caller re-reads the
        rules afterwards to show what's actually left."""
        return "; ".join(shlex.join(c) for c in self.remove_commands())

    # --- reading the current rules ---------------------------------------------
    def ports_allowed(self) -> bool | None:
        """Whether every port in :data:`PORTS` is allowed in, or ``None`` when
        the rules can't be read without root."""
        if self.kind == "firewalld":
            return _firewalld_allows()
        try:
            res = run_host(["cat", UFW_RULES])
        except OSError:
            return None
        if res.returncode != 0:
            return None
        return ufw_rules_allow(res.stdout)

    def rules_stamp(self) -> str:
        """A fingerprint of the current rules for when they can't be read:
        the mtime of ufw's rules file (visible even when its contents aren't).
        ``""`` when there's nothing to fingerprint."""
        if self.kind != "ufw":
            return ""
        try:
            res = run_host(["stat", "-c", "%Y", UFW_RULES])
        except OSError:
            return ""
        return f"ufw:{res.stdout.strip()}" if res.returncode == 0 and res.stdout.strip() else ""


def _firewalld_allows() -> bool | None:
    for port, proto, _ in PORTS:
        try:
            res = run_host(["firewall-cmd", f"--query-port={port}/{proto}"])
        except OSError:
            return None
        if res.returncode == 1:  # "no"
            return False
        if res.returncode != 0:  # daemon not reachable, not authorised...
            return None
    return True


def _port_matches(spec: str, port: int) -> bool:
    if spec == "any":
        return True
    lo, _, hi = spec.partition(":")
    try:
        return int(lo) <= port <= int(hi or lo)
    except ValueError:
        return False


def ufw_rules_allow(text: str, ports=PORTS) -> bool:
    """Do these ``user.rules`` allow every (port, proto) in? Each rule ufw adds
    is summarised on a line like::

        ### tuple ### allow tcp 22000 0.0.0.0/0 any 0.0.0.0/0 in comment=...
        ### tuple ### allow udp 9943:9944 0.0.0.0/0 any 0.0.0.0/0 alvr - in

    i.e. action, proto, dport, src, sport, dst, [app -], direction."""
    allowed: list[tuple[str, str]] = []
    for line in text.splitlines():
        tok = line.split()
        if tok[:4] != ["###", "tuple", "###", "allow"] or len(tok) < 10:
            continue
        if "in" not in tok[9:12]:
            continue
        allowed.append((tok[4], tok[5]))
    return all(
        any(p in (proto, "any") and _port_matches(spec, port) for p, spec in allowed)
        for port, proto, _ in ports
    )


@dataclass(frozen=True)
class Check:
    """What the launch-time check found."""

    firewall: Firewall | None  # None: nothing we know how to open is running
    allowed: bool  # our ports are open (or nothing's blocking them)
    stamp: str  # current rules fingerprint, for remembering an allow


def check(remembered_stamp: str = "") -> Check:
    """Detect the firewall and read its rules. ``remembered_stamp`` is the
    fingerprint stored when ModSync last added its rules; it only matters when
    the rules themselves can't be read."""
    fw = detect()
    if fw is None:
        return Check(None, True, "")
    stamp = fw.rules_stamp()
    allowed = fw.ports_allowed()
    if allowed is None:
        allowed = bool(stamp) and stamp == remembered_stamp
    return Check(fw, allowed, stamp)


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


def allow(fw: Firewall) -> str:
    """Open ModSync's ports in ``fw`` via ``pkexec`` (graphical password prompt).
    Returns the rules fingerprint afterwards, for the caller to remember.

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
    return fw.rules_stamp()


def revoke(fw: Firewall) -> str:
    """Remove the rules :func:`allow` added, via ``pkexec``. Returns the rules
    fingerprint afterwards. Raises :class:`FirewallError` if cancelled."""
    try:
        result = run_host(["pkexec", "sh", "-c", fw.remove_script()])
    except OSError as exc:
        raise FirewallError(f"could not run pkexec: {exc}") from exc
    if result.returncode in (126, 127):
        raise FirewallError("cancelled")
    return fw.rules_stamp()


def manual_instructions(fw: Firewall | None = None, *, remove: bool = False) -> str:
    """What to type by hand, for the README-style hint."""
    fw = fw or Firewall("ufw")
    cmds = fw.remove_commands() if remove else fw.allow_commands()
    return "\n".join("sudo " + shlex.join(c) for c in cmds)
