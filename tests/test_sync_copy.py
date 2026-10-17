""""Copy from another machine" as a sequence of polls: it must never act on a
source that hasn't sent its index, must wait for things to settle, and must
only sync both ways once nothing local is left."""

import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.state import State


class CopyClient:
    """Answers from ``self.folder`` / ``self.remote`` / ``self.connected``,
    which each test moves along between polls."""

    def __init__(self):
        self.folder = {"state": "idle", "needTotalItems": 0, "sequence": 1, "globalFiles": 0,
                       "receiveOnlyTotalItems": 0}
        self.remote = "unknown"
        self.received = 0  # how far the source's list here goes
        self.connected = False
        self.reverts = 0
        self.config = {}

    def my_id(self):
        return "ME"

    def devices(self):
        return [{"deviceID": "ME"}, {"deviceID": "SRC", "name": "Desktop"}]

    def connections(self):
        return {"connections": {"SRC": {"connected": self.connected}}}

    def default_device(self):
        return {"deviceID": "", "name": "", "addresses": ["dynamic"]}

    def put_device(self, device):
        pass

    def default_folder(self):
        return {"id": "", "label": "", "path": "", "devices": [], "type": "sendreceive"}

    def get_folder(self, folder_id):
        return dict(self.config)

    def put_folder(self, folder):
        self.config = dict(folder)

    def folder_status(self, folder_id):
        return dict(self.folder)

    def completion(self, folder_id, device_id=None):
        if device_id:
            return {"completion": 100, "remoteState": self.remote, "sequence": self.received}
        return {"completion": 100, "remoteState": "unknown", "sequence": 0}

    def pending_devices(self):
        return {}

    def revert(self, folder_id):
        self.reverts += 1
        self.folder["receiveOnlyTotalItems"] = 0
        self.folder["sequence"] += 1


class CopyManager:
    running = True

    def __init__(self):
        self.fake = CopyClient()

    def start(self, timeout=0):
        pass

    def stop(self):
        pass

    @contextmanager
    def client(self):
        yield self.fake


class CopyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.instance = self.tmp / "MO2"
        (self.instance / "mods" / "Mine").mkdir(parents=True)
        self.manager = CopyManager()
        self.fake = self.manager.fake
        self.svc = ModSyncService(manager=self.manager)
        self.svc.INDEX_QUIET_S = 0.0  # tests of the list's own gate set it back
        self.svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop", 40), self.instance)

    def poll(self):
        """One step, as the Sync page or the background service takes it."""
        progress = self.svc.advance_copy()
        return progress if progress is None or progress.phase == "done" else self.svc.status().copy

    def source_ready(self):
        self.fake.connected, self.fake.remote, self.fake.received = True, "valid", 40

    def test_the_folder_is_receive_only_from_its_first_write(self):
        self.assertEqual(self.fake.config["type"], "receiveonly")
        self.assertEqual(self.fake.config["versioning"]["type"], "trashcan")
        archive = Path(self.fake.config["versioning"]["fsPath"])
        self.assertEqual(archive.parent, self.instance / ".modsync-before-join")
        self.assertEqual(State.load().copy_phase, "receiving")
        self.assertEqual(State.load().copy_expected, 40)

    def test_valid_but_no_list_yet_is_not_done(self):
        """Syncthing says "valid" as soon as the source shares the folder, and
        the folder reads idle and 100% before any of its list has arrived."""
        self.fake.connected, self.fake.remote = True, "valid"
        for _ in range(4):
            self.assertEqual(self.poll().phase, "indexing")
        self.fake.received = 25  # part of the list: still short of what the code said
        for _ in range(4):
            self.assertEqual(self.poll().phase, "indexing")
        self.assertEqual(self.fake.reverts, 0)
        self.fake.received = 40
        self.assertEqual(self.poll().phase, "receiving")

    def test_the_list_must_stop_growing_first(self):
        self.svc.INDEX_QUIET_S = 60.0
        self.source_ready()
        for _ in range(3):
            self.assertEqual(self.poll().phase, "indexing")
        self.svc._index_seen = (40, self.svc._index_seen[1] - 61)  # a minute without a new batch
        self.assertEqual(self.poll().phase, "receiving")

    def test_without_a_sequence_from_the_source_any_list_must_go_quiet(self):
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop"), self.instance)  # an older version's code
        svc.INDEX_QUIET_S = 60.0
        self.source_ready()
        self.assertEqual(svc.advance_copy().phase, "indexing")  # nothing at all yet
        self.fake.received = 3
        self.assertEqual(svc.advance_copy().phase, "indexing")
        svc._index_seen = (3, svc._index_seen[1] - 61)
        self.assertEqual(svc.advance_copy().phase, "receiving")

    def test_a_source_that_never_reaches_its_number_is_taken_as_it_is_eventually(self):
        self.fake.connected, self.fake.remote, self.fake.received = True, "valid", 10
        self.assertEqual(self.poll().phase, "indexing")
        self.svc._index_seen = (10, self.svc._index_seen[1] - self.svc.INDEX_STALL_S - 1)
        self.assertEqual(self.poll().phase, "receiving")

    def test_status_only_reports(self):
        self.source_ready()
        for _ in range(5):
            self.svc.status()
        self.assertEqual(self.fake.reverts, 0)
        self.assertEqual(State.load().copy_phase, "receiving")
        self.assertIsNone(self.svc._copy_seen)  # nor counts as a quiet poll

    def test_only_one_process_acts_at_a_time(self):
        from modsync import service as service_module

        self.source_ready()
        with service_module._copy_owner() as held:  # another process mid-step
            self.assertTrue(held)
            for _ in range(4):
                self.assertEqual(self.svc.advance_copy().phase, "receiving")
        self.assertEqual(self.fake.reverts, 0)
        self.assertEqual(self.svc.advance_copy().phase, "setting-aside")

    def test_an_unconnected_or_silent_source_is_never_taken_as_done(self):
        # Locally everything reads as complete: nothing is known yet.
        for connected, remote in ((False, "unknown"), (True, "unknown"), (True, "notSharing")):
            self.fake.connected, self.fake.remote = connected, remote
            for _ in range(3):
                self.assertEqual(self.poll().phase, "waiting")
        self.assertEqual(self.fake.reverts, 0)
        self.assertEqual(self.fake.config["type"], "receiveonly")

    def test_waits_for_two_quiet_polls_then_sets_aside_then_promotes(self):
        self.source_ready()
        self.fake.folder.update(state="syncing", needTotalItems=5, globalFiles=9)
        self.assertEqual(self.poll().need_items, 5)
        self.fake.folder.update(state="idle", needTotalItems=0, receiveOnlyTotalItems=2)
        self.assertEqual(self.poll().phase, "receiving")  # first quiet poll
        self.fake.folder["globalFiles"] = 12  # another batch of the index arrived
        self.assertEqual(self.poll().phase, "receiving")
        self.assertEqual(self.fake.reverts, 0)
        self.assertEqual(self.poll().phase, "setting-aside")
        self.assertEqual(self.fake.reverts, 1)
        self.assertEqual(State.load().copy_phase, "setting-aside")
        self.assertEqual(self.poll().phase, "setting-aside")
        done = self.poll()
        self.assertEqual(done.phase, "done")
        self.assertEqual(done.source, "Desktop")
        self.assertEqual(self.fake.config["type"], "sendreceive")
        self.assertEqual(self.fake.config["versioning"]["type"], "")
        loaded = State.load()
        self.assertEqual(loaded.copy_phase, "")
        self.assertEqual(loaded.set_aside, "")  # nothing was archived, so nothing to show
        self.assertFalse((self.instance / ".modsync-before-join").exists())
        self.assertIsNone(self.poll())

    def test_errors_and_pauses_hold_the_copy(self):
        self.source_ready()
        self.fake.folder.update(pullErrors=1)
        for _ in range(3):
            self.assertEqual(self.poll().errors, 1)
        self.assertEqual(self.fake.reverts, 0)

    def test_a_change_made_after_the_revert_is_set_aside_too(self):
        self.source_ready()
        self.poll()
        self.poll()
        self.assertEqual(State.load().copy_phase, "setting-aside")
        self.fake.folder["receiveOnlyTotalItems"] = 1  # MO2 wrote a file
        self.poll()
        self.poll()
        self.assertEqual(self.fake.reverts, 1)
        self.assertEqual(self.fake.config["type"], "receiveonly")

    def test_a_later_revert_never_replaces_what_the_first_set_aside(self):
        """Trash-can versioning keeps one version per path, so a file written
        during the copy must not be reverted into the folder holding the
        version from before the join."""
        archive = Path(self.fake.config["versioning"]["fsPath"])
        self.source_ready()
        self.fake.folder["receiveOnlyTotalItems"] = 1
        self.poll()
        self.poll()  # the first revert
        self.assertEqual(self.fake.reverts, 1)
        (archive / "mods" / "Mine").mkdir(parents=True)
        (archive / "mods" / "Mine" / "a.esp").write_text("from before the join")
        self.fake.folder["receiveOnlyTotalItems"] = 1  # written again during the copy
        self.poll()
        self.poll()
        self.assertEqual(self.fake.reverts, 1)  # not into the same folder
        self.assertEqual(self.fake.config["versioning"]["fsPath"], str(archive / "during-copy-1"))
        self.poll()
        self.poll()
        self.assertEqual(self.fake.reverts, 2)
        self.assertEqual((archive / "mods" / "Mine" / "a.esp").read_text(), "from before the join")
        self.assertEqual(self.poll().phase, "setting-aside")
        self.assertEqual(self.poll().phase, "done")
        self.assertEqual(State.load().set_aside, str(archive))

    def test_a_step_that_waited_for_the_locks_acts_on_the_state_then(self):
        from modsync import service as service_module

        self.source_ready()
        original = service_module._copy_owner

        @contextmanager
        def finished_meanwhile(**kw):
            State.update(copy_phase="", copy_source="", copy_archive="")  # the other process finished
            with original(**kw) as owner:
                yield owner

        with patch.object(service_module, "_copy_owner", finished_meanwhile):
            self.assertIsNone(self.svc.advance_copy())
        self.assertEqual(State.load().copy_phase, "")
        self.assertEqual(self.fake.reverts, 0)

    def test_leaving_waits_for_a_copy_step_under_way(self):
        from modsync import service as service_module

        self.source_ready()
        left = threading.Event()

        def leave():
            ModSyncService(manager=self.manager).stop_sync()
            left.set()

        with service_module._copy_owner() as held:  # the background service mid-step
            self.assertTrue(held)
            thread = threading.Thread(target=leave)
            thread.start()
            self.assertFalse(left.wait(0.3))
            self.assertEqual(State.load().folder_id, "modsync-v")
        thread.join(5)
        self.assertTrue(left.is_set())
        self.assertIsNone(State.load().folder_id)

    def test_an_empty_vault_is_copied(self):
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop", 0), self.instance)
        svc.INDEX_QUIET_S = 0.0
        self.fake.connected, self.fake.remote, self.fake.received = True, "valid", 0
        phases = [svc.advance_copy().phase for _ in range(4)]
        self.assertEqual(phases[-1], "done", phases)

    def test_an_older_code_with_nothing_received_waits_for_the_stall_limit(self):
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop"), self.instance)
        self.assertEqual(State.load().copy_expected, -1)
        svc.INDEX_QUIET_S = 0.0
        self.fake.connected, self.fake.remote, self.fake.received = True, "valid", 0
        self.assertEqual(svc.advance_copy().phase, "indexing")
        svc._index_seen = (0, svc._index_seen[1] - svc.INDEX_STALL_S - 1)
        self.assertEqual(svc.advance_copy().phase, "receiving")

    def test_a_source_emptied_since_its_code_is_taken_as_empty_eventually(self):
        self.fake.connected, self.fake.remote = True, "valid"  # nothing of the 40 ever comes
        self.assertEqual(self.poll().phase, "indexing")
        self.svc._index_seen = (0, self.svc._index_seen[1] - self.svc.INDEX_STALL_S - 1)
        self.assertEqual(self.poll().phase, "receiving")

    def test_a_one_off_status_shows_files_arriving_as_copying(self):
        self.source_ready()
        self.fake.folder.update(state="syncing", needTotalItems=123)
        fresh = ModSyncService(manager=self.manager)  # as `modsync sync status` makes one
        self.assertEqual(fresh.status().copy.phase, "receiving")
        self.fake.folder.update(state="idle", needTotalItems=0)
        self.assertEqual(fresh.status().copy.phase, "indexing")  # nothing to tell by yet

    def test_conflicts_wait_for_the_copy(self):
        from modsync.sync import conflicts

        profile = self.instance / "profiles" / "Default"
        profile.mkdir(parents=True)
        (profile / "modlist.txt").write_text("+Mine\n")
        (profile / "modlist.sync-conflict-20261004-101500-SRCDEVI.txt").write_text("+Theirs\n")
        self.assertEqual(self.svc.conflicts(), [])
        self.assertEqual(self.svc.status().conflicts, 0)
        with self.assertRaisesRegex(RuntimeError, "still being copied"):
            self.svc.resolve_conflicts(conflicts.find(self.instance), "other")
        self.assertEqual((profile / "modlist.txt").read_text(), "+Mine\n")

    def test_versioning_cleared_elsewhere_is_not_taken_for_the_working_directory(self):
        cwd = self.tmp / "cwd"
        cwd.mkdir()
        (cwd / "junk").write_text("x")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)
        archive = Path(self.fake.config["versioning"]["fsPath"])
        self.fake.config["versioning"] = {"type": "trashcan", "fsPath": ""}  # in Syncthing's own UI
        self.svc._revert_again(self.fake, "modsync-v", archive)
        self.assertEqual(self.fake.reverts, 1)
        self.assertFalse((archive / "during-copy-1").exists())

    def test_a_relative_instance_path_is_made_absolute(self):
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop", 40), "MO2")
        self.assertEqual(State.load().instance_path, str(self.instance))
        self.assertEqual(self.fake.config["path"], str(self.instance))
        self.assertTrue(Path(self.fake.config["versioning"]["fsPath"]).is_absolute())
        self.assertTrue(State.load().copy_archive.startswith(str(self.instance)))

    def test_the_sequence_given_out_is_unknown_until_the_first_scan(self):
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        svc.create_vault(self.instance)
        self.fake.folder.update(state="scanning", sequence=0)
        self.assertIsNone(svc._folder_sequence())
        self.fake.folder.update(state="idle")
        self.assertEqual(svc._folder_sequence(), 0)
        self.fake.folder.update(state="scanning", sequence=7)
        self.assertEqual(svc._folder_sequence(), 7)

    def test_resumes_from_disk_after_a_restart(self):
        self.source_ready()
        self.poll()
        self.poll()
        again = ModSyncService(manager=self.manager)  # the app restarted
        again.INDEX_QUIET_S = 0.0
        self.assertTrue(again.state.copying)
        again.advance_copy()
        again.advance_copy()
        self.assertEqual(again.state.copy_phase, "")
        self.assertEqual(self.fake.config["type"], "sendreceive")

    def test_a_kept_archive_is_offered_then_can_be_deleted(self):
        archive = Path(self.fake.config["versioning"]["fsPath"])
        (archive / "mods" / "Mine").mkdir(parents=True)
        (archive / "mods" / "Mine" / "m.esp").write_text("x")
        self.source_ready()
        for _ in range(4):
            progress = self.poll()
        self.assertEqual(progress.set_aside, 1)
        self.assertEqual(Path(State.load().set_aside), archive)
        self.svc.dismiss_set_aside(delete=True)
        self.assertFalse(archive.exists())
        self.assertEqual(State.load().set_aside, "")

    def test_playing_is_blocked_until_the_copy_is_done(self):
        self.assertIn("still being copied from Desktop", self.svc.launch_check().blocked)

    def test_a_stale_service_pausing_after_the_copy_finished_keeps_it_finished(self):
        stale = ModSyncService(manager=self.manager)  # the background service, loaded mid-copy
        self.source_ready()
        for _ in range(4):
            self.poll()
        self.assertEqual(State.load().copy_phase, "")
        stale.pause_sync()
        loaded = State.load()
        self.assertEqual((loaded.copy_phase, loaded.copy_source), ("", ""))
        self.assertTrue(loaded.sync_paused)
        self.assertEqual(self.fake.config["type"], "sendreceive")

    def test_merge_joins_both_ways_at_once(self):
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop"), self.instance, merge=True)
        self.assertEqual(self.fake.config["type"], "sendreceive")
        self.assertNotIn("versioning", self.fake.config)
        self.assertFalse(svc.state.copying)
        self.assertIsNone(svc.status().copy)

    def test_leaving_mid_copy_forgets_it(self):
        self.svc.stop_sync()
        loaded = State.load()
        self.assertFalse(loaded.copying)
        self.assertEqual((loaded.copy_phase, loaded.copy_archive), ("", ""))


class LaunchCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.instance = self.tmp / "MO2"
        (self.instance / "profiles" / "Default").mkdir(parents=True)
        State(instance_path=str(self.instance), folder_id="modsync-v").save()
        self.manager = CopyManager()
        self.svc = ModSyncService(manager=self.manager)

    def test_nothing_to_say_when_in_sync(self):
        check = self.svc.launch_check()
        self.assertEqual((check.blocked, check.warnings), ("", []))

    def test_warns_about_changes_still_arriving_and_conflicts(self):
        self.manager.fake.folder.update(state="syncing", needTotalItems=3)
        (self.instance / "profiles" / "Default" / "modlist.sync-conflict-20261004-101500-ABCDEFG.txt").write_text("")
        check = self.svc.launch_check()
        self.assertFalse(check.blocked)
        self.assertIn("3 changes from another machine", check.warnings[0])
        self.assertIn("1 file was changed on two machines", check.warnings[1])

    def test_a_paused_vault_says_nothing_about_pending_changes(self):
        State.update(sync_paused=True)
        self.manager.fake.folder.update(needTotalItems=3)
        self.assertEqual(self.svc.launch_check().warnings, [])

    def test_does_not_start_syncthing_to_ask(self):
        self.manager.running = False
        self.manager.start = Mock()
        self.assertEqual(self.svc.launch_check().blocked, "")
        self.manager.start.assert_not_called()

    def test_not_syncing_skips_syncthing_entirely(self):
        State(instance_path=str(self.instance)).save()
        svc = ModSyncService(manager=self.manager)
        svc.status = None  # would raise if called
        self.assertEqual(svc.launch_check().warnings, [])

    def test_a_background_service_started_without_a_vault_picks_up_a_copy(self):
        from modsync.serve import Server

        State(instance_path=str(self.instance)).save()
        background = ModSyncService(manager=self.manager)
        background.INDEX_QUIET_S = 0.0
        server = Server(background, log=lambda _l: None, notify=lambda *_a: None)
        server.tick()
        app = ModSyncService(manager=self.manager)  # the app joins meanwhile
        app.join_vault(PairingCode("SRC", "modsync-v", "Desktop", 40), self.instance)
        fake = self.manager.fake
        fake.connected, fake.remote, fake.received = True, "valid", 40
        for _ in range(6):
            server.tick()
        self.assertEqual(State.load().copy_phase, "")
        self.assertEqual(fake.config["type"], "sendreceive")

    def test_pause_and_resume(self):
        self.svc.pause_sync()
        self.assertTrue(self.manager.fake.config["paused"])
        self.assertTrue(State.load().sync_paused)
        self.assertEqual(self.svc.status().folder_state, "paused")
        self.svc.pause_sync(False)
        self.assertFalse(self.manager.fake.config["paused"])
        self.assertFalse(State.load().sync_paused)
