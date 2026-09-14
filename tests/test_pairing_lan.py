"""Loopback tests for the PIN-authenticated LAN pairing handshake."""

import socket
import threading
import unittest

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
        self.assertEqual(peer, HOST)
        self.assertEqual(results.get("peer"), JOINER)

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
