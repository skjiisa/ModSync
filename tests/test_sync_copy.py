""""Copy from another machine" as a sequence of polls: it must never act on a
source that hasn't sent its index, must wait for things to settle, and must
only sync both ways once nothing local is left."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

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
        return {"completion": 100, "remoteState": self.remote if device_id else "unknown"}

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
        self.svc.join_vault(PairingCode("SRC", "modsync-v", "Desktop"), self.instance)

    def poll(self):
        return self.svc.status().copy

    def test_the_folder_is_receive_only_from_its_first_write(self):
        self.assertEqual(self.fake.config["type"], "receiveonly")
        self.assertEqual(self.fake.config["versioning"]["type"], "trashcan")
        archive = Path(self.fake.config["versioning"]["fsPath"])
        self.assertEqual(archive.parent, self.instance / ".modsync-before-join")
        self.assertEqual(State.load().copy_phase, "receiving")

    def test_an_unconnected_or_silent_source_is_never_taken_as_done(self):
        # Locally everything reads as complete: nothing is known yet.
        for connected, remote in ((False, "unknown"), (True, "unknown"), (True, "notSharing")):
            self.fake.connected, self.fake.remote = connected, remote
            for _ in range(3):
                self.assertEqual(self.poll().phase, "waiting")
        self.assertEqual(self.fake.reverts, 0)
        self.assertEqual(self.fake.config["type"], "receiveonly")

    def test_waits_for_two_quiet_polls_then_sets_aside_then_promotes(self):
        self.fake.connected, self.fake.remote = True, "valid"
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
        self.fake.connected, self.fake.remote = True, "valid"
        self.fake.folder.update(pullErrors=1)
        for _ in range(3):
            self.assertEqual(self.poll().errors, 1)
        self.assertEqual(self.fake.reverts, 0)

    def test_a_change_made_after_the_revert_is_set_aside_too(self):
        self.fake.connected, self.fake.remote = True, "valid"
        self.poll()
        self.poll()
        self.assertEqual(State.load().copy_phase, "setting-aside")
        self.fake.folder["receiveOnlyTotalItems"] = 1  # MO2 wrote a file
        self.poll()
        self.poll()
        self.assertEqual(self.fake.reverts, 1)
        self.assertEqual(self.fake.config["type"], "receiveonly")

    def test_resumes_from_disk_after_a_restart(self):
        self.fake.connected, self.fake.remote = True, "valid"
        self.poll()
        self.poll()
        again = ModSyncService(manager=self.manager)  # the app restarted
        self.assertTrue(again.state.copying)
        again.status()
        again.status()
        self.assertEqual(again.state.copy_phase, "")
        self.assertEqual(self.fake.config["type"], "sendreceive")

    def test_a_kept_archive_is_offered_then_can_be_deleted(self):
        archive = Path(self.fake.config["versioning"]["fsPath"])
        (archive / "mods" / "Mine").mkdir(parents=True)
        (archive / "mods" / "Mine" / "m.esp").write_text("x")
        self.fake.connected, self.fake.remote = True, "valid"
        for _ in range(4):
            progress = self.poll()
        self.assertEqual(progress.set_aside, 1)
        self.assertEqual(Path(State.load().set_aside), archive)
        self.svc.dismiss_set_aside(delete=True)
        self.assertFalse(archive.exists())
        self.assertEqual(State.load().set_aside, "")

    def test_playing_is_blocked_until_the_copy_is_done(self):
        self.assertIn("still being copied from Desktop", self.svc.launch_check().blocked)

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
        self.svc.state.sync_paused = True
        self.manager.fake.folder.update(needTotalItems=3)
        self.assertEqual(self.svc.launch_check().warnings, [])

    def test_not_syncing_skips_syncthing_entirely(self):
        State(instance_path=str(self.instance)).save()
        svc = ModSyncService(manager=self.manager)
        svc.status = None  # would raise if called
        self.assertEqual(svc.launch_check().warnings, [])

    def test_pause_and_resume(self):
        self.svc.pause_sync()
        self.assertTrue(self.manager.fake.config["paused"])
        self.assertTrue(State.load().sync_paused)
        self.assertEqual(self.svc.status().folder_state, "paused")
        self.svc.pause_sync(False)
        self.assertFalse(self.manager.fake.config["paused"])
        self.assertFalse(State.load().sync_paused)
