"""Loopback tests for the PIN-authenticated LAN pairing handshake."""

import socket
import threading
import unittest
from dataclasses import replace

from modsync import pairing_lan
from modsync.pairing_lan import Announcement, PairError, PairPayload, _recv, _send

HOST = PairPayload("HOST-DEVICE-ID", "modsync-abc123", "Deck")
JOINER = PairPayload("JOINER-DEVICE-ID")


def _run_host(pin: str, results: dict, **kw) -> threading.Thread:
    """Start host_pairing on a thread; the announcement lands in results["ann"]."""
    ready = threading.Event()

    def on_ready(ann: Announcement) -> None:
        results["ann"] = Announcement(ann.name, "127.0.0.1", ann.port, ann.session)
        ready.set()

    def run() -> None:
        try:
            results["peer"] = pairing_lan.host_pairing(
                HOST, "deck", pin, on_ready=on_ready, timeout=10, **kw
            )
        except Exception as exc:  # surfaced by the test
            results["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    self_ok = ready.wait(5)
    assert self_ok, "host never became ready"
    return t


class Handshake(unittest.TestCase):
    def test_garbage_spake_message_is_a_failed_attempt_not_a_crash(self):
        results: dict = {}
        t = _run_host("123456", results, max_attempts=1)
        ann = results["ann"]
        with socket.create_connection((ann.host, ann.port), timeout=5) as sock:
            _send(sock, b"not a spake2 message")
            _recv(sock)
        t.join(5)
        self.assertIsInstance(results.get("error"), PairError)
        self.assertIn("too many failed attempts", str(results["error"]))

    def test_matching_pin_exchanges_payloads(self):
        results: dict = {}
        t = _run_host("123456", results)
        peer = pairing_lan.join_pairing(results["ann"], JOINER, "123456", timeout=5)
        t.join(5)
        # Each side also learns where it reached the other, for Syncthing.
        self.assertEqual(peer, replace(HOST, host="127.0.0.1"))
        self.assertEqual(results.get("peer"), replace(JOINER, host="127.0.0.1"))

    def test_local_host_field_is_never_sent(self):
        self.assertNotIn(b"host", replace(HOST, host="10.0.0.1").to_bytes())

    def test_wrong_pin_is_rejected_on_both_sides(self):
        results: dict = {}
        t = _run_host("123456", results, max_attempts=1)
        with self.assertRaises(PairError):
            pairing_lan.join_pairing(results["ann"], JOINER, "654321", timeout=5)
        t.join(5)
        self.assertIsInstance(results.get("error"), PairError)
        self.assertNotIn("peer", results)

    def test_reflected_frames_do_not_authenticate(self):
        """A joiner that doesn't know the PIN echoes every host frame back.

        Confirmation and payload MACs are role-bound, so the host must reject
        this even though every echoed value is one it computed itself.
        """
        results: dict = {}
        t = _run_host("123456", results, max_attempts=1)
        ann = results["ann"]
        from spake2 import SPAKE2_B

        with socket.create_connection((ann.host, ann.port), timeout=5) as sock:
            # A well-formed party-B message under a guessed (wrong) PIN, so the
            # host derives *some* key, then echo everything it sends.
            _send(sock, SPAKE2_B(b"000000").start())
            _recv(sock)
            for _ in range(3):  # confirm, payload body, payload mac
                try:
                    _send(sock, _recv(sock))
                except (PairError, OSError):
                    break
        t.join(5)
        self.assertNotIn("peer", results)
        self.assertIsInstance(results.get("error"), PairError)


class HostPort(unittest.TestCase):
    def test_host_uses_the_fixed_pairing_port(self):
        results: dict = {}
        stop = threading.Event()
        try:
            t = _run_host("123456", results, stop=stop)
        except AssertionError:
            self.skipTest("could not host (port 21029 in use?)")
        try:
            self.assertEqual(results["ann"].port, pairing_lan.PAIR_PORT)
        finally:
            stop.set()
            t.join(5)

    def test_host_falls_back_to_an_ephemeral_port_when_busy(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            blocker.bind(("0.0.0.0", pairing_lan.PAIR_PORT))
            blocker.listen(1)
        except OSError as exc:
            blocker.close()
            self.skipTest(f"port already in use: {exc}")
        results: dict = {}
        stop = threading.Event()
        try:
            t = _run_host("123456", results, stop=stop)
            ann = results["ann"]
            self.assertNotEqual(ann.port, pairing_lan.PAIR_PORT)
            self.assertGreater(ann.port, 0)
            # ...and pairing still works on the fallback port.
            peer = pairing_lan.join_pairing(ann, JOINER, "123456", timeout=5)
            self.assertEqual(peer, replace(HOST, host="127.0.0.1"))
        finally:
            stop.set()
            blocker.close()
            t.join(5)


class ManualAddress(unittest.TestCase):
    def test_bare_host_uses_the_pairing_port(self):
        ann = Announcement.manual(" 192.168.1.20 ")
        self.assertEqual((ann.host, ann.port), ("192.168.1.20", pairing_lan.PAIR_PORT))
        self.assertEqual(ann.name, "192.168.1.20")

    def test_host_and_port(self):
        ann = Announcement.manual("deck.local:40123")
        self.assertEqual((ann.host, ann.port), ("deck.local", 40123))

    def test_invalid_addresses_are_rejected(self):
        for bad in ("", "   ", "host:abc", "host:0", "host:70000", ":5"):
            with self.subTest(bad=bad), self.assertRaises(PairError):
                Announcement.manual(bad)

    def test_manual_announcement_can_join(self):
        results: dict = {}
        t = _run_host("123456", results)
        ann = Announcement.manual(f"127.0.0.1:{results['ann'].port}")
        peer = pairing_lan.join_pairing(ann, JOINER, "123456", timeout=5)
        t.join(5)
        self.assertEqual(peer, replace(HOST, host="127.0.0.1"))


class BroadcastTargets(unittest.TestCase):
    def test_always_includes_limited_and_loopback_broadcast(self):
        targets = pairing_lan._broadcast_targets()
        self.assertIn("255.255.255.255", targets)
        self.assertIn("127.255.255.255", targets)
        self.assertEqual(len(targets), len(set(targets)))
        for t in targets:
            socket.inet_aton(t)  # every entry is a dotted quad


class Discovery(unittest.TestCase):
    def test_announcement_is_discoverable_on_loopback(self):
        stop = threading.Event()
        t = threading.Thread(
            target=pairing_lan._announce_loop,
            args=("deck", "127.0.0.1", 4242, "sess1", stop),
            daemon=True,
        )
        t.start()
        try:
            found = pairing_lan.discover(timeout=2.0)
        except PairError as exc:  # port in use by another ModSync on this box
            self.skipTest(str(exc))
        finally:
            stop.set()
        matches = [a for a in found if a.session == "sess1"]
        self.assertEqual(len(matches), 1)
        self.assertEqual((matches[0].name, matches[0].port), ("deck", 4242))


if __name__ == "__main__":
    unittest.main()
