"""What keeps Steam's Play from starting Skyrim without the window
(``ModSyncService.play_attention``): what Home would put ahead of Play."""

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import gameversion
from modsync.mo2 import usvfs
from modsync.service import LaunchCheck, ModSyncService
from modsync.state import State
from tests.ui_support import fake_game_status


class PlayAttentionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(tmp.name) / "config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        State(instance_path=tmp.name).save()
        self.game = fake_game_status()
        self.check = LaunchCheck()
        self.runtime: list[str] = []
        self.usvfs = usvfs.Status("not-needed", "")
        for p in (patch.object(ModSyncService, "game_status", lambda svc, refresh_index=True: self.game),
                  patch.object(ModSyncService, "launch_check", lambda svc: self.check),
                  patch.object(ModSyncService, "prefix_runtime_problems", lambda svc: self.runtime),
                  patch.object(ModSyncService, "usvfs_status", lambda svc: self.usvfs)):
            p.start()
            self.addCleanup(p.stop)
        self.svc = ModSyncService(manager=object())

    def test_nothing_to_decide(self):
        self.assertIsNone(self.svc.play_attention())

    def test_no_instance_goes_on_to_what_steam_would_run(self):
        State(instance_path=None).save()
        self.svc = ModSyncService(manager=object())
        self.runtime = ["msvcp140.dll"]
        self.assertIsNone(self.svc.play_attention())

    def test_what_home_puts_ahead_of_play(self):
        v = gameversion.GameVersion.parse
        cases = {
            "runtime": (lambda: setattr(self, "runtime", ["msvcp140.dll 14.29"]), "Visual C++"),
            "usvfs": (lambda: setattr(self, "usvfs", usvfs.Status("available", "", can_apply=True, can_restore=True)),
                      "ARM64 fix"),
            "version": (lambda: setattr(self, "game", dataclasses.replace(self.game, expected=v("1.6.1170"))),
                        "Your mods need Skyrim 1.6.1170"),
            "skse": (lambda: setattr(self, "game", dataclasses.replace(
                self.game, skse_runtimes=["1.6.1170", "1.7.104"], skse_runtime=v("1.7.104"))), "SKSE"),
            "update": (lambda: setattr(self, "game", dataclasses.replace(self.game, steam_is_current=False)),
                       "update waiting"),
            "copy": (lambda: setattr(self, "check", LaunchCheck(blocked="Mods are still being copied.")),
                     "still being copied"),
            "arriving": (lambda: setattr(self, "check", LaunchCheck(warnings=["3 changes haven't arrived yet."])),
                         "haven't arrived"),
        }
        for key, (make, said) in cases.items():
            with self.subTest(key):
                self.setUp()
                make()
                self.assertIn(said, self.svc.play_attention() or "")

    def test_a_queued_pin_or_missing_skse_does_not_hold_it(self):
        self.game = dataclasses.replace(self.game, steam_is_current=False, pending_pin=True)
        self.assertIsNone(self.svc.play_attention())


if __name__ == "__main__":
    unittest.main()
