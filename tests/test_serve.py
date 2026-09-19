"""The serve loop does only what the setup calls for: pins and update-watching
always; Syncthing only when a vault exists."""

import unittest
from unittest.mock import patch

from modsync import gameversion, launchhook
from modsync.serve import Server
from modsync.service import DeviceStatus, PinOutcome, SyncStatus
from modsync.state import State


class FakeService:
    def __init__(self, state, installed="1.6.1170", expected="1.6.1170"):
        self.state = state
        self.installed = installed
        self.expected = expected
        self.pin = None
        self.started = 0
        self.stopped = 0
        self.status_calls = 0

    def game_version_check(self):
        v = lambda s: gameversion.GameVersion.parse(s) if s else None  # noqa: E731
        return gameversion.VersionCheck(v(self.installed), v(self.expected))

    def ensure_running(self):
        self.started += 1

    def shutdown(self):
        self.stopped += 1

    def apply_pending_pin(self):
        pin, self.pin = self.pin, None
        return pin

    def accept_pending(self):
        return ["ABCDEFGHIJKLMNOP"]

    def status(self):
        self.status_calls += 1
        return SyncStatus("ME", self.state.folder_id, True, "idle", 100.0, [DeviceStatus("X", "deck", True)])


class ServerTests(unittest.TestCase):
    def make(self, state, **kw):
        svc = FakeService(state, **kw)
        lines, notes = [], []
        server = Server(svc, log=lines.append, notify=lambda t, b: notes.append((t, b)))
        return svc, server, lines, notes

    def test_without_vault_never_starts_syncthing(self):
        svc, server, lines, _ = self.make(State(instance_path="/mo2"))
        server.start()
        server.tick()
        self.assertEqual(svc.started, 0)
        self.assertEqual(svc.status_calls, 0)
        self.assertIn("Watching for Steam updates — press Ctrl-C to stop.", lines)

    def test_with_vault_syncs(self):
        svc, server, lines, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        server.start()
        server.tick()
        self.assertEqual(svc.started, 1)
        self.assertEqual(svc.status_calls, 1)
        self.assertTrue(any("accepted new device" in l for l in lines))
        self.assertTrue(any("100% in sync" in l for l in lines))

    def test_queued_pin_is_applied_without_a_vault(self):
        svc, server, lines, notes = self.make(State())
        svc.pin = PinOutcome(True, False, [], "Pinned.")
        server.start()
        server.tick()
        self.assertIn("  ✓ Pinned.", lines)
        self.assertEqual(notes[0][0], "Steam pin applied")

    def test_queued_launch_hook_change_is_applied(self):
        svc, server, lines, notes = self.make(State())
        with patch.object(launchhook, "apply_pending", return_value="Launch hook selected for the game."):
            server.start()
            server.tick()
        self.assertIn("  Launch hook selected for the game.", lines)
        self.assertEqual(notes[0], ("ModSync launch hook", "Launch hook selected for the game."))

    def test_steam_update_is_noticed(self):
        svc, server, lines, notes = self.make(State(instance_path="/mo2"))
        server.start()
        svc.installed = "1.7.104"
        for _ in range(13):  # the version is re-read every 12th tick
            server.tick()
        self.assertEqual(len(notes), 1)
        self.assertIn("changed from 1.6.1170 to 1.7.104", notes[0][1])
        self.assertIn("built for 1.6.1170", notes[0][1])
        for _ in range(13):  # no repeat while nothing changes
            server.tick()
        self.assertEqual(len(notes), 1)

    def test_mismatch_is_reported_at_start(self):
        _, server, lines, _ = self.make(State(instance_path="/mo2"), installed="1.7.104")
        server.start()
        self.assertTrue(any("built for 1.6.1170" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
