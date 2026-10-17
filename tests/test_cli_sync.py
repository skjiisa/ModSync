"""`modsync sync status` and `modsync sync conflicts`, and how often the
background count walks the instance for conflicts."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from modsync.__main__ import main
from modsync.service import ModSyncService
from modsync.state import State
from tests.test_sync_copy import CopyManager

STAMP = "sync-conflict-20261004-101500-SRCDEVI"


def run_cli(*args):
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(list(args))
    return code, out.getvalue()


class _Vault(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.instance = self.tmp / "MO2"
        self.profile = self.instance / "profiles" / "Default"
        self.profile.mkdir(parents=True)
        State(instance_path=str(self.instance), folder_id="modsync-v").save()
        self.manager = CopyManager()
        self.manager.fake.devices = lambda: [{"deviceID": "ME"}, {"deviceID": "SRCDEVICE-1", "name": "Desktop"}]
        self.manager.fake.connections = lambda: {"connections": {"SRCDEVICE-1": {"connected": True}}}
        services = patch("modsync.service.ModSyncService",
                         side_effect=lambda *a, **k: ModSyncService(manager=self.manager))
        services.start()
        self.addCleanup(services.stop)

    def write(self, rel, text):
        path = self.instance / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def profile_conflict(self):
        for name, here, there in (("modlist.txt", "+Mine\n", "+Theirs\n"), ("plugins.txt", "*Mine.esp\n",
                                                                         "*Theirs.esp\n")):
            self.write(f"profiles/Default/{name}", here)
            stem, ext = name.rsplit(".", 1)
            self.write(f"profiles/Default/{stem}.{STAMP}.{ext}", there)


class SyncStatusCli(_Vault):
    def test_off_without_a_vault(self):
        State(instance_path=str(self.instance)).save()
        code, out = run_cli("sync", "status")
        self.assertEqual(code, 0)
        self.assertIn("Sync: off", out)

    def test_shows_the_vault_machines_and_conflicts(self):
        self.profile_conflict()
        code, out = run_cli("sync", "status")
        self.assertEqual(code, 0)
        self.assertIn("Vault:      modsync-v", out)
        self.assertIn("State:      idle, 100% here", out)
        self.assertIn("Machine:    Desktop  connected", out)
        self.assertIn("Conflicts:  2  ('modsync sync conflicts' lists them)", out)

    def test_paused(self):
        State.update(sync_paused=True)
        self.manager.fake.config = {"paused": True}
        code, out = run_cli("sync", "status")
        self.assertIn("State:      paused", out)


class SyncConflictsCli(_Vault):
    def test_none(self):
        self.assertEqual(run_cli("sync", "conflicts"), (0, "No conflicts.\n"))

    def test_lists_each_with_what_differs_and_when_the_other_changed(self):
        self.profile_conflict()
        for copy in self.profile.glob("*.sync-conflict-*"):
            stamp = datetime(2026, 10, 3, 21, 40).timestamp()
            os.utime(copy, (stamp, stamp))
        self.write("mods/Tweaks/tweaks.ini", "[General]\nfScale=1.0\n")
        self.write(f"mods/Tweaks/tweaks.{STAMP}.ini", "[General]\nfScale=2.5\n")
        code, out = run_cli("sync", "conflicts")
        self.assertEqual(code, 0)
        self.assertIn("3 file(s) changed on two machines", out)
        self.assertIn("profiles/Default/modlist.txt  (other version last changed 2026-10-03 21:40)", out)
        self.assertIn("    Only in the other version: Theirs.", out)
        self.assertIn("mods/Tweaks/tweaks.ini", out)
        self.assertIn("    Set differently: [General] fScale (1.0 in use, 2.5 in the other).", out)

    def test_keep_other_takes_the_whole_profile(self):
        self.profile_conflict()
        code, out = run_cli("sync", "conflicts", "keep", "other", "profiles/Default/plugins.txt")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.profile / "modlist.txt").read_text(), "+Theirs\n")
        self.assertEqual((self.profile / "plugins.txt").read_text(), "*Theirs.esp\n")
        self.assertIn("Kept the other version of profiles/Default/modlist.txt.", out)
        archived = sorted(p.name for p in (self.instance / ".modsync-conflicts").rglob("*.txt"))
        self.assertEqual(archived, ["modlist.txt", "plugins.txt"])
        self.assertEqual(run_cli("sync", "conflicts")[1], "No conflicts.\n")

    def test_keep_current_by_the_conflict_copys_path(self):
        self.write("overwrite/a.json", "{}")
        copy = self.write(f"overwrite/a.{STAMP}.json", "{ }")
        code, out = run_cli("sync", "conflicts", "keep", "current", str(copy))
        self.assertEqual(code, 0, out)
        self.assertFalse(copy.exists())
        self.assertEqual((self.instance / "overwrite" / "a.json").read_text(), "{}")

    def test_unknown_file_and_bad_arguments(self):
        self.profile_conflict()
        code, out = run_cli("sync", "conflicts", "keep", "other", "profiles/Default/loadorder.txt")
        self.assertEqual(code, 1)
        self.assertIn("No conflict for profiles/Default/loadorder.txt", out)
        self.assertEqual(run_cli("sync", "conflicts", "keep", "both", "x")[0], 2)

    def test_refuses_while_mo2_runs(self):
        self.profile_conflict()
        with patch("modsync.mo2.launch.Launcher.running", return_value=True):
            code, out = run_cli("sync", "conflicts", "keep", "other", "profiles/Default/modlist.txt")
        self.assertEqual(code, 1)
        self.assertIn("Close Mod Organizer 2", out)
        self.assertEqual((self.profile / "modlist.txt").read_text(), "+Mine\n")


class ConflictRescanPacing(_Vault):
    def test_a_slow_walk_waits_longer_before_the_next(self):
        svc = ModSyncService(manager=self.manager)
        clock = [1000.0]
        walks = []

        def find(path):
            walks.append(clock[0])
            clock[0] += self.walk_seconds  # the walk itself takes this long
            return []

        with patch("modsync.service.time.monotonic", side_effect=lambda: clock[0]), \
                patch("modsync.service.conflicts.find", side_effect=find):
            self.walk_seconds = 0.01
            svc._conflict_count()
            clock[0] += 29
            svc._conflict_count()
            self.assertEqual(len(walks), 1)  # fast: every 30 s
            clock[0] += 2
            svc._conflict_count()
            self.assertEqual(len(walks), 2)
            self.walk_seconds = 2.0  # mods/ got big: about 1% of the time
            clock[0] += 31
            svc._conflict_count()
            clock[0] += 150
            svc._conflict_count()
            self.assertEqual(len(walks), 3)
            clock[0] += 60
            svc._conflict_count()
            self.assertEqual(len(walks), 4)
            self.walk_seconds = 60.0  # never more than 10 minutes apart
            clock[0] += 300
            svc._conflict_count()
            clock[0] += 539  # 599 s after that walk started
            svc._conflict_count()
            self.assertEqual(len(walks), 5)
            clock[0] += 2
            svc._conflict_count()
            self.assertEqual(len(walks), 6)
            svc.conflicts()  # asked for directly: always walks
            self.assertEqual(len(walks), 7)
