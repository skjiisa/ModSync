"""What syncing says before a game other than Skyrim starts
(``ModSyncService.game_launch_check``), and the CLI's Play and Open MO2,
which ask it the way the window does. Syncthing is faked."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import cli, mo2games
from modsync.service import LaunchCheck, ModSyncService
from modsync.state import State

PEER = "PEER"
STARFIELD = "starfield-mo2"
CONFLICT = "Plugins.sync-conflict-20261116-120000-ABCDEFG.txt"


class FakeClient:
    def __init__(self, folders: dict, status: dict, connected=(PEER,)):
        self.folders, self.status, self.connected = folders, status, connected

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def my_id(self):
        return "ME"

    def connections(self):
        return {"connections": {d: {"connected": True} for d in self.connected}}

    def get_folder(self, folder_id):
        return self.folders.get(folder_id, {"id": folder_id, "devices": [{"deviceID": "ME"}, {"deviceID": PEER}]})

    def folder_status(self, folder_id):
        return self.status.get(folder_id, {"state": "idle", "needTotalItems": 0})


class FakeManager:
    """A Syncthing running here (``running``), or one to attach to."""

    def __init__(self, client=None, *, running=True, attachable=False):
        self._client = client or FakeClient({}, {})
        self.running = running
        self.attachable = attachable
        self.attached = 0

    def attach(self):
        self.attached += 1
        return self.attachable

    def client(self):
        return self._client


class GameLaunchCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "config"),
                                          "XDG_STATE_HOME": str(self.tmp / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.starfield = self.instance("starfield")
        self.cyberpunk = self.instance("cyberpunk")
        State(game_folders={STARFIELD: str(self.starfield), "cp2077-mo2": str(self.cyberpunk)}).save()

    def instance(self, name: str) -> Path:
        root = self.tmp / name
        (root / "profiles" / "Default").mkdir(parents=True)
        (root / "profiles" / "Default" / "Plugins.txt").write_text("*Mod.esm\n")
        return root

    def check(self, manager=None, keys=(STARFIELD,)) -> LaunchCheck:
        return ModSyncService(manager=manager or FakeManager()).game_launch_check(keys)

    def test_nothing_synced_for_the_game_says_nothing_and_asks_nobody(self):
        manager = FakeManager(running=False)
        check = ModSyncService(manager=manager).game_launch_check(("baldursgate3-mo2",))
        self.assertEqual((check.sync, check.attention), ("off", ""))
        self.assertEqual(manager.attached, 0)

    def test_a_copy_kept_for_other_machines_is_not_the_games_folder_here(self):
        State(held_folders={STARFIELD: str(self.starfield)}).save()
        client = FakeClient({}, {f"modsync-{STARFIELD}": {"state": "syncing", "needTotalItems": 4}})
        self.assertEqual(self.check(FakeManager(client)).sync, "off")

    def test_changes_still_arriving_for_the_game_ask_first(self):
        client = FakeClient({}, {f"modsync-{STARFIELD}": {"state": "syncing", "needTotalItems": 3}})
        check = self.check(FakeManager(client))
        self.assertEqual(check.sync, "known")
        self.assertEqual(len(check.warnings), 1)
        self.assertIn("3 changes to Starfield mods haven't arrived yet.", check.attention)
        self.assertEqual(check.conflicts, 0)
        self.assertFalse(check.blocked)

    def test_a_folder_still_scanning_says_some_changes(self):
        client = FakeClient({}, {f"modsync-{STARFIELD}": {"state": "sync-preparing", "needTotalItems": 0}})
        self.assertIn("Some changes to Starfield mods", self.check(FakeManager(client)).attention)

    def test_changes_on_a_machine_that_isnt_connected_say_so(self):
        client = FakeClient({}, {f"modsync-{STARFIELD}": {"state": "idle", "needTotalItems": 1}}, connected=())
        self.assertIn("the machine that has them isn't connected", self.check(FakeManager(client)).attention)

    def test_a_paused_folder_has_nothing_to_wait_for(self):
        folder = {"id": f"modsync-{STARFIELD}", "paused": True, "devices": [{"deviceID": PEER}]}
        client = FakeClient({f"modsync-{STARFIELD}": folder},
                            {f"modsync-{STARFIELD}": {"state": "paused", "needTotalItems": 9}})
        check = self.check(FakeManager(client))
        self.assertEqual((check.sync, check.warnings), ("known", []))

    def test_files_changed_on_two_machines_ask_first(self):
        (self.starfield / "profiles" / "Default" / CONFLICT).write_text("*Other.esm\n")
        check = self.check()
        self.assertEqual(check.conflicts, 1)
        self.assertIn("1 file was changed on two machines at once", check.attention)

    def test_another_games_folder_never_holds_it_up(self):
        (self.cyberpunk / "profiles" / "Default" / CONFLICT).write_text("*Other.esm\n")
        client = FakeClient({}, {"modsync-cp2077-mo2": {"state": "syncing", "needTotalItems": 12},
                                 "modsync-dfu-mods": {"state": "syncing", "needTotalItems": 2}})
        check = self.check(FakeManager(client))
        self.assertEqual((check.sync, check.attention), ("known", ""))
        self.assertIn("12 changes to Cyberpunk 2077 mods",
                      self.check(FakeManager(client), keys=("cp2077-mo2",)).warnings[0])

    def test_a_folder_being_stopped_is_left_out(self):
        State(game_folders={STARFIELD: str(self.starfield)}, declined_folders=[STARFIELD]).save()
        client = FakeClient({}, {f"modsync-{STARFIELD}": {"state": "syncing", "needTotalItems": 3}})
        self.assertEqual(self.check(FakeManager(client)).sync, "off")

    def test_without_a_syncthing_here_it_says_it_cant_tell_and_starts_none(self):
        (self.starfield / "profiles" / "Default" / CONFLICT).write_text("*Other.esm\n")
        manager = FakeManager(running=False)
        check = self.check(manager)
        self.assertEqual(manager.attached, 1)  # one already running would have been asked
        self.assertEqual(check.sync, "unavailable")
        self.assertEqual(check.conflicts, 1)  # what's on disk still counts
        self.assertEqual(len(check.warnings), 1)

    def test_one_to_attach_to_is_asked(self):
        client = FakeClient({}, {f"modsync-{STARFIELD}": {"state": "idle", "needTotalItems": 2}})
        check = self.check(FakeManager(client, running=False, attachable=True))
        self.assertEqual(check.sync, "known")
        self.assertIn("2 changes", check.attention)

    def test_a_syncthing_that_doesnt_answer_is_unavailable_not_clean(self):
        class Broken(FakeClient):
            def folder_status(self, folder_id):
                raise OSError("connection refused")

        check = self.check(FakeManager(Broken({}, {})))
        self.assertEqual((check.sync, check.warnings), ("unavailable", []))

    def test_daggerfall_checks_its_mods_and_saves(self):
        from modsync import dfu

        saves = self.tmp / "saves"
        saves.mkdir()
        State(game_folders={"dfu-saves": str(saves)}).save()
        client = FakeClient({}, {"modsync-dfu-saves": {"state": "syncing", "needTotalItems": 1}})
        check = self.check(FakeManager(client), keys=dfu.LAUNCH_FOLDERS)
        self.assertIn("1 change to Daggerfall Unity saves haven't arrived yet", check.attention)


class CliLaunchTests(unittest.TestCase):
    """``modsync games <key> play|open``: what syncing says, and --anyway."""

    def setUp(self):
        self.game = mo2games.get("starfield")
        for p in (patch.object(self.game, "refresh_hook", return_value=[]),
                  patch.object(self.game, "version_problem", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, args, check: LaunchCheck):
        with patch.object(ModSyncService, "game_launch_check", return_value=check) as asked, \
                patch.object(self.game, "launch", return_value="Starting Starfield.") as launch:
            code = cli._managed_game(self.game, args)
        asked.assert_called_once_with(("starfield-mo2",))
        return code, launch

    def test_nothing_to_say_plays(self):
        code, launch = self.run_cli(["play"], LaunchCheck(sync="known"))
        self.assertEqual(code, 0)
        self.assertTrue(launch.call_args.kwargs["play"])

    def test_changes_arriving_stop_it_and_say_how_to_go_on(self):
        check = LaunchCheck(warnings=["3 changes to Starfield mods haven't arrived yet."], sync="known")
        with patch("builtins.print") as out:
            code, launch = self.run_cli(["open"], check)
        said = "\n".join(str(c.args[0]) for c in out.call_args_list)
        self.assertEqual(code, 1)
        launch.assert_not_called()
        self.assertIn("wait: 3 changes", said)
        self.assertIn("modsync games starfield open --anyway", said)

    def test_conflicts_point_to_choosing_versions(self):
        check = LaunchCheck(warnings=["1 file was changed on two machines at once."], sync="known", conflicts=1)
        with patch("builtins.print") as out:
            code, _launch = self.run_cli(["play"], check)
        self.assertEqual(code, 1)
        self.assertIn("modsync sync conflicts", "\n".join(str(c.args[0]) for c in out.call_args_list))

    def test_anyway_says_what_it_found_and_plays(self):
        check = LaunchCheck(warnings=["3 changes haven't arrived yet."], sync="known")
        with patch("builtins.print") as out:
            code, launch = self.run_cli(["play", "--anyway"], check)
        self.assertEqual(code, 0)
        launch.assert_called_once()
        self.assertIn("warning: 3 changes", "\n".join(str(c.args[0]) for c in out.call_args_list))

    def test_without_syncthing_it_says_so_and_plays(self):
        with patch("builtins.print") as out:
            code, launch = self.run_cli(["play"], LaunchCheck(sync="unavailable"))
        self.assertEqual(code, 0)
        launch.assert_called_once()
        self.assertIn("can't tell what is still on its way", "\n".join(str(c.args[0]) for c in out.call_args_list))

    def test_nothing_synced_plays_without_a_word(self):
        with patch("builtins.print") as out:
            code, launch = self.run_cli(["play"], LaunchCheck())
        self.assertEqual(code, 0)
        launch.assert_called_once()
        self.assertEqual([c.args[0] for c in out.call_args_list], ["Starting Starfield."])

    def test_a_check_that_fails_never_keeps_the_game_from_starting(self):
        with patch.object(ModSyncService, "game_launch_check", side_effect=RuntimeError("boom")), \
                patch.object(self.game, "launch", return_value="Starting Starfield.") as launch, \
                patch("builtins.print"):
            self.assertEqual(cli._managed_game(self.game, ["play"]), 0)
        launch.assert_called_once()

    def test_an_unknown_flag_is_usage(self):
        with patch("builtins.print"):
            self.assertEqual(cli._managed_game(self.game, ["play", "--now"]), 2)


if __name__ == "__main__":
    unittest.main()
