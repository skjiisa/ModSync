"""Choosing an instance is independent of syncing: it persists without ever
starting Syncthing, and leaving a vault keeps the instance."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from modsync import gameversion
from modsync.service import ModSyncService
from modsync.state import State


class FakeClient:
    def __init__(self, manager):
        self.manager = manager

    def my_id(self):
        return "ME"

    def devices(self):
        return [{"deviceID": "ME"}, {"deviceID": "PEER"}]

    def delete_folder(self, folder_id):
        self.manager.deleted_folders.append(folder_id)

    def delete_device(self, device_id):
        self.manager.deleted_devices.append(device_id)


class FakeManager:
    def __init__(self):
        self.running = False
        self.started = 0
        self.deleted_folders = []
        self.deleted_devices = []

    def start(self, timeout=0):
        self.running = True
        self.started += 1

    def stop(self):
        self.running = False

    @contextmanager
    def client(self):
        yield FakeClient(self)


class InstanceLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.instance = self.tmp / "MO2"
        self.instance.mkdir()
        self.manager = FakeManager()
        # No game on this box: recording the initial version is a no-op.
        self.no_game = patch.object(gameversion, "find_game_dir", return_value=None)
        self.no_game.start()
        self.addCleanup(self.no_game.stop)

    def test_choose_instance_persists_without_syncthing(self):
        svc = ModSyncService(manager=self.manager)
        svc.choose_instance(self.instance)
        self.assertEqual(self.manager.started, 0)
        loaded = State.load()
        self.assertTrue(loaded.has_instance)
        self.assertFalse(loaded.syncing)
        self.assertEqual(loaded.instance_path, str(self.instance))
        self.assertEqual(loaded.instance_label, "MO2")

    def test_choose_instance_records_version_from_skse(self):
        (self.instance / "mods" / "SKSE" / "Root").mkdir(parents=True)
        (self.instance / "mods" / "SKSE" / "Root" / "skse64_1_6_1170.dll").write_bytes(b"")
        with patch.object(gameversion, "find_game_dir", return_value=self.tmp / "game"):
            (self.tmp / "game").mkdir()
            with patch.object(gameversion, "installed_version", return_value=gameversion.GameVersion.parse("1.7.104")):
                ModSyncService(manager=self.manager).choose_instance(self.instance)
        meta = gameversion.VaultMeta.load(self.instance)
        self.assertIsNotNone(meta)
        self.assertEqual(str(meta.version), "1.6.1170")
        self.assertEqual(meta.set_from, "skse")

    def test_cannot_switch_instance_while_syncing(self):
        State(instance_path=str(self.instance), folder_id="modsync-1").save()
        svc = ModSyncService(manager=self.manager)
        with self.assertRaisesRegex(RuntimeError, "stop syncing"):
            svc.choose_instance(self.tmp / "Other")
        svc.choose_instance(self.instance, label="Same")  # same path is fine
        self.assertEqual(State.load().instance_label, "Same")

    def test_stop_sync_keeps_the_instance(self):
        State(instance_path=str(self.instance), folder_id="modsync-1", instance_label="Deck").save()
        svc = ModSyncService(manager=self.manager)
        svc.stop_sync()
        self.assertEqual(self.manager.deleted_folders, ["modsync-1"])
        self.assertEqual(self.manager.deleted_devices, ["PEER"])
        loaded = State.load()
        self.assertTrue(loaded.has_instance)
        self.assertFalse(loaded.syncing)
        self.assertEqual(loaded.instance_label, "Deck")

    def test_reset_forgets_everything(self):
        State(instance_path=str(self.instance), folder_id="modsync-1").save()
        svc = ModSyncService(manager=self.manager)
        svc.reset()
        loaded = State.load()
        self.assertFalse(loaded.has_instance)
        self.assertFalse(loaded.syncing)
        self.assertEqual(self.manager.deleted_folders, ["modsync-1"])

    def test_forget_instance_without_sync_never_starts_syncthing(self):
        State(instance_path=str(self.instance)).save()
        svc = ModSyncService(manager=self.manager)
        svc.forget_instance()
        self.assertEqual(self.manager.started, 0)
        self.assertFalse(State.load().has_instance)


if __name__ == "__main__":
    unittest.main()
