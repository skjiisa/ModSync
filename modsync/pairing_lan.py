"""LAN pairing — find another ModSync machine on the local network and exchange
Syncthing identities over a PIN-authenticated channel, so the user never types a
long pairing code (a real pain between a Steam Deck and a PC).

Flow:
  * The **host** (the machine that has the vault) calls :func:`host_pairing`: it
    announces itself over UDP broadcast and listens on a TCP port, running SPAKE2
    as the password party A. The UI shows a 6-digit PIN.
  * The **joiner** calls :func:`discover` to list announcing machines, then
    :func:`join_pairing` with the PIN read off the host's screen: it connects and
    runs SPAKE2 as party B.

Security model: the PIN seeds SPAKE2, which yields a shared key only if both
sides used the same PIN. We key-confirm and then MAC the exchanged identity with
that key, so an attacker on the LAN who doesn't know the PIN can neither
impersonate a side nor substitute device ids — and SPAKE2 permits only one online
PIN guess per handshake (we also cap attempts). Device ids are certificate
fingerprints, not secrets, so we authenticate rather than encrypt them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

# One port number for both halves of pairing, so a single firewall rule
# ("allow 21029") covers it: announcements go out over UDP and the PIN
# handshake runs over TCP. The host falls back to an ephemeral TCP port only if
# 21029 is already taken (say, a second ModSync on the same box).
DISCOVERY_PORT = 21029  # UDP broadcast port for ModSync pairing announcements
PAIR_PORT = 21029  # TCP port the host listens on for the PIN handshake
_MAGIC = "modsync-pair-v1"
_MAX_FRAME = 65536

FIREWALL_HINT = (
    "If the machine with the mods runs a firewall, allow port 21029 (TCP and UDP) "
    "for pairing, plus TCP 22000 and UDP 21027 for Syncthing."
)


class PairError(RuntimeError):
    pass


@dataclass
class Announcement:
    name: str
    host: str
    port: int
    session: str

    @classmethod
    def manual(cls, address: str) -> "Announcement":
        """An announcement typed in by hand (``host`` or ``host:port``) for when
        broadcast discovery can't cross the network (VLANs, AP client isolation)."""
        address = address.strip()
        host, port = address, PAIR_PORT
        if address.count(":") == 1:  # host:port (a bare IPv6 address has more colons)
            host, _, port_s = address.partition(":")
            try:
                port = int(port_s)
            except ValueError:
                raise PairError(f"not a valid address: {address!r}") from None
        host = host.strip("[]")
        if not host or not 0 < port < 65536:
            raise PairError(f"not a valid address: {address!r}")
        return cls(host, host, port, f"manual:{host}:{port}")


@dataclass
class PairPayload:
    """The identity each side hands the other to wire up Syncthing."""

    device_id: str
    folder_id: str = ""
    label: str = ""
    # The IP we reached the peer on — filled in locally after the handshake,
    # never sent. Lets Syncthing dial it straight away instead of waiting on
    # its own LAN discovery (which firewalls drop just like ours).
    host: str = ""

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"device_id": self.device_id, "folder_id": self.folder_id, "label": self.label},
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "PairPayload":
        d = json.loads(data.decode())
        return cls(d["device_id"], d.get("folder_id", ""), d.get("label", ""))


def make_pin() -> str:
    """A 6-digit PIN, shown grouped as e.g. ``042 815`` by the UI."""
    return f"{secrets.randbelow(1_000_000):06d}"


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET-1: no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _broadcast_targets() -> list[str]:
    """Where to send announcements: the limited broadcast plus each interface's
    own directed broadcast (e.g. ``192.168.71.255``).

    ``255.255.255.255`` alone only leaves through the default-route interface,
    and some Wi-Fi mesh / access points forward one kind of broadcast but not
    the other, so we send both. Interface lookup is Linux-only (``SIOCGIFBRDADDR``);
    anywhere else we quietly fall back to the limited broadcast.
    """
    targets = ["255.255.255.255", "127.255.255.255"]
    try:
        import fcntl

        siocgifbrdaddr = 0x8919
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _idx, name in socket.if_nameindex():
                ifreq = struct.pack("256s", name.encode()[:15])
                try:
                    res = fcntl.ioctl(sock.fileno(), siocgifbrdaddr, ifreq)
                except OSError:  # no IPv4 address / no broadcast (lo, tun, wg)
                    continue
                addr = socket.inet_ntoa(res[20:24])
                if addr not in targets and not addr.startswith("0."):
                    targets.append(addr)
        finally:
            sock.close()
    except (ImportError, OSError, AttributeError):
        pass
    return targets


# --- length-prefixed framing over TCP ---------------------------------------

def _send(sock: socket.socket, data: bytes) -> None:
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recvn(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise PairError("connection closed mid-handshake")
        buf += chunk
    return bytes(buf)


def _recv(sock: socket.socket) -> bytes:
    (n,) = struct.unpack(">I", _recvn(sock, 4))
    if n > _MAX_FRAME:
        raise PairError("oversized frame")
    return _recvn(sock, n)


# --- the PIN-authenticated handshake ----------------------------------------

def _mac(key: bytes, label: bytes, *parts: bytes) -> bytes:
    h = hmac.new(key, label, hashlib.sha256)
    for p in parts:
        h.update(p)
    return h.digest()


def _handshake(sock: socket.socket, pin: str, payload: PairPayload, *, is_host: bool) -> PairPayload:
    from spake2 import SPAKE2_A, SPAKE2_B, SPAKEError

    party = (SPAKE2_A if is_host else SPAKE2_B)(pin.encode())
    my_msg = party.start()
    _send(sock, my_msg)
    their_msg = _recv(sock)
    try:
        key = party.finish(their_msg)
    except SPAKEError as exc:  # malformed / wrong-side message: a bad peer, not a crash
        raise PairError(f"invalid pairing message: {exc}") from exc

    # Bind the transcript to both SPAKE2 messages, ordered host-first on both ends.
    transcript = (my_msg + b"|" + their_msg) if is_host else (their_msg + b"|" + my_msg)

    # Every MAC is bound to the sender's role. Without this a peer that doesn't
    # know the PIN could simply reflect our own confirm/payload frames back at us
    # and pass both checks (the expected values would be exactly what we sent).
    mine, theirs = (b"host", b"joiner") if is_host else (b"joiner", b"host")

    # Key confirmation: catches a wrong PIN (or a MITM) before any identity is sent.
    _send(sock, _mac(key, b"modsync-confirm-" + mine, transcript))
    if not hmac.compare_digest(_recv(sock), _mac(key, b"modsync-confirm-" + theirs, transcript)):
        raise PairError("PIN did not match")

    # Authenticated identity exchange (device ids aren't secret, so MAC not encrypt).
    body = payload.to_bytes()
    _send(sock, body)
    _send(sock, _mac(key, b"modsync-payload-" + mine, transcript, body))
    their_body = _recv(sock)
    their_mac = _recv(sock)
    if not hmac.compare_digest(
        their_mac, _mac(key, b"modsync-payload-" + theirs, transcript, their_body)
    ):
        raise PairError("peer failed authentication")
    return PairPayload.from_bytes(their_body)


# --- discovery (UDP broadcast) ----------------------------------------------

def _announce_loop(name: str, host: str, port: int, session: str, stop: threading.Event) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps(
        {"magic": _MAGIC, "name": name, "host": host, "port": port, "session": session}
    ).encode()
    targets = _broadcast_targets()
    log.info(
        "announcing %r (%s:%d, session %s) to udp/%d via %s",
        name, host, port, session, DISCOVERY_PORT, ", ".join(targets),
    )
    failed: set[str] = set()
    try:
        while not stop.is_set():
            for target in targets:
                try:
                    sock.sendto(msg, (target, DISCOVERY_PORT))
                except OSError as exc:
                    if target not in failed:  # log each broken target once
                        failed.add(target)
                        log.warning("cannot announce to %s: %s", target, exc)
            stop.wait(1.0)
    finally:
        sock.close()


def discover(timeout: float = 3.0) -> list[Announcement]:
    """Listen for announcing ModSync machines on the LAN for ``timeout`` seconds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        log.warning("cannot bind udp/%d for discovery: %s", DISCOVERY_PORT, exc)
        raise PairError(f"cannot listen for ModSync machines: {exc}") from exc
    sock.settimeout(0.4)
    log.info("scanning for ModSync machines on udp/%d for %.1fs", DISCOVERY_PORT, timeout)
    found: dict[str, Announcement] = {}
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            try:
                d = json.loads(data.decode())
                if d.get("magic") != _MAGIC:
                    log.debug("ignoring non-ModSync packet from %s", addr[0])
                    continue
                ann = Announcement(d["name"], d.get("host") or addr[0], int(d["port"]), d["session"])
            except (ValueError, KeyError, TypeError):
                log.debug("ignoring malformed announcement from %s", addr[0])
                continue
            if ann.session not in found:
                log.info("found %r at %s:%d (from %s)", ann.name, ann.host, ann.port, addr[0])
            found[ann.session] = ann
    finally:
        sock.close()
    log.info("scan finished: %d machine(s)", len(found))
    return list(found.values())


# --- host / join entry points -----------------------------------------------

def host_pairing(
    payload: PairPayload,
    name: str,
    pin: str,
    *,
    timeout: float = 120.0,
    max_attempts: int = 5,
    on_ready: Callable[[Announcement], None] | None = None,
    stop: threading.Event | None = None,
) -> PairPayload:
    """Announce on the LAN and wait for a joiner to complete the PIN handshake.

    Blocks until a peer pairs (returns their :class:`PairPayload`), the timeout
    elapses, attempts are exhausted, or ``stop`` is set.
    """
    stop = stop or threading.Event()
    session = secrets.token_hex(4)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("0.0.0.0", PAIR_PORT))
    except OSError as exc:
        log.warning("tcp/%d busy (%s); pairing on an ephemeral port instead", PAIR_PORT, exc)
        srv.bind(("0.0.0.0", 0))
    srv.listen(1)
    srv.settimeout(0.5)
    tcp_port = srv.getsockname()[1]
    log.info("hosting pairing session %s on tcp/%d", session, tcp_port)

    announcer = threading.Thread(
        target=_announce_loop,
        args=(name, _local_ip(), tcp_port, session, stop),
        daemon=True,
    )
    announcer.start()
    if on_ready is not None:
        on_ready(Announcement(name, _local_ip(), tcp_port, session))

    attempts = 0
    deadline = time.monotonic() + timeout
    try:
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                conn, (peer_ip, _) = srv.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(15)
                try:
                    peer = _handshake(conn, pin, payload, is_host=True)
                except (PairError, OSError) as exc:  # wrong PIN / flaky peer
                    attempts += 1
                    log.info("pairing attempt %d from %s failed: %s", attempts, peer_ip, exc)
                    if attempts >= max_attempts:
                        raise PairError("too many failed attempts; start pairing again") from exc
                    continue
                peer.host = peer_ip
                log.info("paired with %r (%s) at %s", peer.label, peer.device_id[:13], peer_ip)
                return peer
        log.info("pairing session %s ended without a peer", session)
        raise PairError("timed out waiting for a machine to pair")
    finally:
        stop.set()
        srv.close()


def join_pairing(
    announcement: Announcement,
    payload: PairPayload,
    pin: str,
    *,
    timeout: float = 15.0,
) -> PairPayload:
    """Connect to an announced host and complete the PIN handshake."""
    log.info("joining %r at %s:%d", announcement.name, announcement.host, announcement.port)
    try:
        sock = socket.create_connection((announcement.host, announcement.port), timeout=timeout)
    except OSError as exc:
        log.warning("could not reach %s:%d: %s", announcement.host, announcement.port, exc)
        raise PairError(
            f"could not reach {announcement.name} on port {announcement.port}: {exc}. "
            + FIREWALL_HINT
        ) from exc
    with sock:
        sock.settimeout(timeout)
        peer = _handshake(sock, pin, payload, is_host=False)
    peer.host = announcement.host
    return peer
