"""The serve loop does only what the setup calls for: pins and update-watching
always; Syncthing only when a vault exists."""

import unittest
from unittest.mock import patch

from modsync import gameversion, launchhook, mo2games, nxm
from modsync.serve import Server
from modsync.service import DeviceStatus, PinOutcome, SyncStatus
from modsync.state import NewerStateError, State


class FakeService:
    def __init__(self, state, installed="1.6.1170", expected="1.6.1170"):
        self.state = state
        self.installed = installed
        self.expected = expected
        self.pin = None
        self.started = 0
        self.stopped = 0
        self.status_calls = 0
        self.upkept = 0
        self.queued = False
        self.waits = []

    holds_for_play = False
    pause_fails = False

    def pause_for_play(self):
        if self.pause_fails:
            raise RuntimeError("Syncthing didn't answer")
        self.holds_for_play = True
        self.state.paused_for_play = ["modsync-1"]
        return ["modsync-1"]

    def resume_after_play(self):
        self.holds_for_play = False
        resumed, self.state.paused_for_play = self.state.paused_for_play, []
        return resumed

    def steam_change_queued(self):
        return self.queued

    def watch_sync_events(self, timeout):
        self.waits.append(timeout)
        return None

    def upkeep(self):
        self.upkept += 1

    def advance_copy(self):
        return None

    def game_version_check(self):
        v = lambda s: gameversion.GameVersion.parse(s) if s else None  # noqa: E731
        return gameversion.VersionCheck(v(self.installed), v(self.expected))

    def ensure_running(self):
        self.started += 1

    def reload_state(self):
        return self.state

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
    def setUp(self):
        # Starting refreshes the hooks and ModSync's nxm:// handler: never this
        # machine's real Steam, Heroic or desktop settings.
        for p in (patch.object(launchhook, "upgrade", return_value=None),
                  patch.object(launchhook, "hooked_appids", return_value=[]),
                  patch.object(mo2games, "refresh_hooks", return_value=[]),
                  patch.object(nxm, "refresh", return_value=[])):
            p.start()
            self.addCleanup(p.stop)

    def make(self, state, **kw):
        svc = FakeService(state, **kw)
        lines, notes = [], []
        server = Server(svc, log=lines.append, notify=lambda t, b: notes.append((t, b)))
        server._detect_play = lambda: self.game  # never this machine's processes
        return svc, server, lines, notes

    game = False

    def test_a_newer_modsync_taking_over_the_state_stops_it(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2"))

        def newer():
            raise NewerStateError("state.json is from a newer ModSync")

        svc.reload_state = newer
        with patch.object(Server, "start", lambda self: None), self.assertRaises(NewerStateError):
            server.run(interval=0)
        self.assertEqual(svc.stopped, 1)  # and its Syncthing with it

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
        self.assertTrue(any("accepted new device" in l for l in lines))
        self.assertEqual(svc.upkept, 1)

    def test_the_sync_status_is_read_only_during_a_copy(self):
        # Syncthing calls the folder status expensive, and nobody looks at it here.
        svc, server, lines, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        server.tick()
        self.assertEqual(svc.status_calls, 0)
        svc.state.copy_phase = "receiving"
        server.tick()
        self.assertEqual(svc.status_calls, 1)
        self.assertTrue(any("100% in sync" in l for l in lines))

    def test_folder_upkeep_runs_once_a_minute(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        now = [0.0]
        server._clock = lambda: now[0]
        for t in (0, 5, 30, 59):
            now[0] = t
            server.tick()
        self.assertEqual(svc.upkept, 1)
        now[0] = 60
        server.tick()
        self.assertEqual(svc.upkept, 2)

    def test_it_waits_long_unless_something_waits_on_steam_or_a_copy(self):
        from modsync import serve

        svc, server, _, _ = self.make(State(instance_path="/mo2"))
        self.assertEqual(server.interval(), serve.IDLE_S)
        svc.queued = True
        self.assertEqual(server.interval(), serve.BUSY_S)
        svc.queued = False
        svc.state.copy_phase = "receiving"
        self.assertEqual(server.interval(), serve.BUSY_S)

    def quick_wait(self, server, seconds, *, woken_at=None, queued_at=None, svc=None):
        """Run ``server.wait`` on a fake clock that each sleep or wait moves on.
        Returns the seconds that passed."""
        from modsync import serve

        now = [0.0]
        server._clock = lambda: now[0]

        def advance(t):
            now[0] += t
            if queued_at is not None and now[0] >= queued_at:
                svc.queued = True

        class Woken:
            def wait(self, t):
                if woken_at is not None and now[0] + t >= woken_at:
                    now[0] = max(now[0], woken_at)
                    return True
                advance(t)
                return False

            def clear(self):
                pass

        server._woken = Woken()
        server._start_watch = lambda: None
        with patch.object(serve.time, "sleep", side_effect=advance):
            server.wait(seconds)
        return now[0]

    def test_while_syncing_the_watch_wakes_it(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        self.assertEqual(self.quick_wait(server, 30, woken_at=12), 12)
        self.assertEqual(self.quick_wait(server, 30, woken_at=0.2), 1)  # a burst wakes it once a second at most
        self.assertTrue(server._watch_on.is_set())
        self.assertEqual(self.quick_wait(server, 30), 30)

    def test_a_steam_change_queued_mid_wait_ends_it(self):
        # Steam is gone only for seconds on a switch between Gaming Mode and the desktop.
        for state in (State(instance_path="/mo2"), State(instance_path="/mo2", folder_id="modsync-1")):
            svc, server, _, _ = self.make(state)
            self.assertLessEqual(self.quick_wait(server, 30, queued_at=7, svc=svc), 10)

    def test_a_busy_wait_runs_its_course(self):
        from modsync import serve

        svc, server, _, _ = self.make(State(instance_path="/mo2"))
        svc.queued = True
        self.assertEqual(self.quick_wait(server, serve.BUSY_S), serve.BUSY_S)

    def test_without_a_vault_it_does_not_watch(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2"))
        self.assertEqual(self.quick_wait(server, 30), 30)
        self.assertFalse(server._watch_on.is_set())

    def test_the_watch_thread_wakes_the_loop(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        replies = [True]

        def watch(timeout):
            if replies:
                return replies.pop()
            server._watch_on.clear()  # park the thread
            return False

        svc.watch_sync_events = watch
        server._watch_on.set()
        server._start_watch()
        self.assertTrue(server._woken.wait(5))

    def test_a_finished_copy_is_announced_once(self):
        from modsync.service import CopyProgress

        svc, server, lines, notes = self.make(State(instance_path="/mo2", folder_id="modsync-1",
                                                    set_aside="/mo2/.modsync-before-join/x",
                                                    copy_phase="receiving"))
        steps = [CopyProgress("receiving", "Desktop", need_items=4), CopyProgress("done", "Desktop", set_aside=2)]

        def advance():
            step = steps.pop(0)
            if step.phase == "done":
                svc.state.copy_phase = ""  # as the real one leaves it
            return step

        svc.advance_copy = advance
        svc.status = lambda: SyncStatus("ME", "modsync-1", True, "idle", 100.0, [],
                                        copy=CopyProgress("receiving", "Desktop", need_items=4))
        server.tick()
        self.assertIn("copying from Desktop: receiving", lines[-1])
        server.tick()
        self.assertEqual(notes, [("Copy finished", "This machine now has the same mods as Desktop. 2 file(s) "
                                  "that were only here are kept in /mo2/.modsync-before-join/x.")])
        server.tick()  # the copy is over: no second notice, no status read
        self.assertEqual(len(notes), 1)

    def test_a_game_pauses_syncing_until_it_closes(self):
        svc, server, lines, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        now = [0.0]
        server._clock = lambda: now[0]
        self.game = True
        server.tick()
        self.assertTrue(svc.holds_for_play)
        self.assertIn("  A game is running: paused 1 folder(s) until it closes.", lines)
        self.assertEqual(svc.upkept, 0)  # nothing else while it plays
        svc.accept_pending = lambda: self.fail("no pairing while a game runs")
        clock = server._clock
        self.assertEqual(self.quick_wait(server, 30), 30)
        self.assertFalse(server._watch_on.is_set())  # a plain sleep: no watching Syncthing's events
        server._clock = clock
        now[0] = 10
        self.game = False
        server.tick()  # checked at most every 30 s
        self.assertTrue(svc.holds_for_play)
        now[0] = 30
        del svc.accept_pending
        server.tick()
        self.assertFalse(svc.holds_for_play)
        self.assertEqual(svc.state.paused_for_play, [])
        self.assertIn("  No game is running: resumed 1 folder(s).", lines)
        self.assertEqual(svc.upkept, 1)

    def test_a_window_opened_and_closed_mid_game_does_not_end_the_pause(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        now = [0.0]
        server._clock = lambda: now[0]
        self.game = True
        server.tick()
        svc.state.paused_for_play = []  # a window opened (resuming them) and closed within 30 s
        now[0] = 10
        server.tick()
        self.assertFalse(svc.holds_for_play)
        now[0] = 30
        server.tick()
        self.assertTrue(svc.holds_for_play)  # paused again
        self.assertEqual(svc.state.paused_for_play, ["modsync-1"])

    def test_never_pauses_mid_copy_or_without_anything_syncing(self):
        self.game = True
        svc, server, _, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1", copy_phase="receiving"))
        server.tick()
        self.assertFalse(svc.holds_for_play)
        svc, server, _, _ = self.make(State(instance_path="/mo2"))
        server._detect_play = lambda: self.fail("no need to look")
        server.tick()
        self.assertFalse(svc.holds_for_play)

    def test_a_failed_pause_keeps_syncing(self):
        self.game = True
        svc, server, lines, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1"))
        svc.pause_fails = True
        server.tick()
        self.assertFalse(server._playing)
        self.assertEqual(svc.upkept, 1)
        self.assertTrue(any("could not pause for the game" in l for l in lines))

    def test_folders_left_paused_by_a_crash_are_resumed(self):
        svc, server, _, _ = self.make(State(instance_path="/mo2", folder_id="modsync-1",
                                            paused_for_play=["modsync-1"]))
        server.tick()
        self.assertEqual(svc.state.paused_for_play, [])

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
        now = [0.0]
        server._clock = lambda: now[0]
        server.start()
        server.tick()
        svc.installed = "1.7.104"
        now[0] = 30
        server.tick()
        self.assertEqual(notes, [])  # the version is re-read once a minute
        now[0] = 60
        server.tick()
        self.assertEqual(len(notes), 1)
        self.assertIn("changed from 1.6.1170 to 1.7.104", notes[0][1])
        self.assertIn("built for 1.6.1170", notes[0][1])
        for t in (120, 180):  # no repeat while nothing changes
            now[0] = t
            server.tick()
        self.assertEqual(len(notes), 1)

    def test_mismatch_is_reported_at_start(self):
        _, server, lines, _ = self.make(State(instance_path="/mo2"), installed="1.7.104")
        server.start()
        self.assertTrue(any("built for 1.6.1170" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
