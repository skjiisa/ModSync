"""The games' folders and machine-wide pairing, against an in-memory Syncthing."""

import copy
import dataclasses
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from modsync.pairing_code import PairingCode
from modsync.service import ModSyncService
from modsync.state import State
from modsync.sync import folders


class FakeSyncthing:
    """Enough of Syncthing's config and pending lists for the folder logic."""

    def __init__(self, me="ME"):
        self.me = me
        self.devices = {me: {"deviceID": me, "name": "this one"}}
        self.folders: dict[str, dict] = {}
        self.pending_devices: dict[str, dict] = {}
        self.pending_folders: dict[str, dict] = {}
        self.running = True
        self.refuse_delete = False

    # SyncthingManager
    def start(self, timeout=0):
        self.running = True

    def stop(self):
        pass

    def device_id(self):
        return self.me

    @contextmanager
    def client(self):
        yield _Client(self)


class _Client:
    def __init__(self, st: FakeSyncthing):
        self.st = st

    def my_id(self):
        return self.st.me

    def devices(self):
        return list(copy.deepcopy(self.st.devices).values())

    def default_device(self):
        return {"deviceID": "", "name": "", "addresses": ["dynamic"]}

    def put_device(self, device):
        self.st.devices[device["deviceID"]] = copy.deepcopy(device)
        self.st.pending_devices.pop(device["deviceID"], None)

    def delete_device(self, device_id):
        self.st.devices.pop(device_id, None)

    def default_folder(self):
        return {"id": "", "label": "", "path": "", "type": "sendreceive", "devices": []}

    def get_folder(self, folder_id):
        if folder_id not in self.st.folders:
            raise KeyError(folder_id)
        return copy.deepcopy(self.st.folders[folder_id])

    def put_folder(self, folder):
        self.st.folders[folder["id"]] = copy.deepcopy(folder)
        self.st.pending_folders.pop(folder["id"], None)

    def delete_folder(self, folder_id):
        if self.st.refuse_delete:
            raise OSError("Syncthing said no")
        if folder_id not in self.st.folders:
            raise KeyError(folder_id)  # Syncthing answers 404
        del self.st.folders[folder_id]

    def folders(self):
        return list(copy.deepcopy(self.st.folders).values())

    def pending_devices(self):
        return copy.deepcopy(self.st.pending_devices)

    def pending_folders(self):
        return copy.deepcopy(self.st.pending_folders)

    def folder_status(self, folder_id):
        return {"state": "idle", "needTotalItems": 0}

    def completion(self, folder_id, device_id=None):
        return {"completion": 100}

    def connections(self):
        return {"connections": {}}


def folder_devices(st: FakeSyncthing, folder_id: str) -> set[str]:
    return {d["deviceID"] for d in st.folders[folder_id]["devices"]}


class GameFolderBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.root: Path | None = self.tmp / "Game" / "Mods"
        self.spec = folders.FolderSpec("test-mods", "Test mods (ModSync)", lambda: self.root,
                                       ("// test", "!/keep", "/*"))
        p = patch.object(folders, "specs", lambda: [self.spec])
        p.start()
        self.addCleanup(p.stop)
        self.st = FakeSyncthing()
        self.service = ModSyncService(manager=self.st)


class ShareAndStop(GameFolderBase):
    def test_share_with_every_paired_machine_and_stop_keeps_files(self):
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        root = self.service.share_game_folder("test-mods")
        self.assertEqual(root, self.root)
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.root))
        self.assertEqual((self.root / ".stignore").read_text(), "// test\n!/keep\n/*\n")
        self.assertEqual(State.load().game_folders, {"test-mods": str(self.root)})
        (self.root / "keep").write_text("mine")
        self.service.stop_game_folder("test-mods")
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.assertEqual((self.root / "keep").read_text(), "mine")
        self.assertEqual(State.load().game_folders, {})
        self.assertEqual(State.load().declined_folders, ["test-mods"])
        self.service.share_game_folder("test-mods")  # starting again un-declines it
        self.assertEqual(State.load().declined_folders, [])

    def test_a_game_not_set_up_here_cannot_be_shared(self):
        self.root = None
        with self.assertRaisesRegex(RuntimeError, "set the game up"):
            self.service.share_game_folder("test-mods")
        with self.assertRaisesRegex(RuntimeError, "no folder called"):
            self.service.share_game_folder("nope")

    def test_a_stop_syncthing_refuses_finishes_later(self):
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        self.service.share_game_folder("test-mods")
        self.st.refuse_delete = True
        self.assertFalse(self.service.stop_game_folder("test-mods"))
        self.assertIn("modsync-test-mods", self.st.folders)
        self.assertTrue(folders.stopping("test-mods"))
        self.assertTrue(self.service.status().game_folders[0].stopping)
        # Meanwhile it's not handed to new machines, nor taken up again.
        self.service.pair(PairingCode("NEW", "", "New"))
        self.assertNotIn("NEW", folder_devices(self.st, "modsync-test-mods"))
        self.st.refuse_delete = False
        self.service._refresh_game_folders(force=True)  # the next retry (pair() above used one)
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.assertEqual(State.load().game_folders, {})
        self.assertEqual(State.load().declined_folders, ["test-mods"])

    def test_a_stop_with_syncthing_down_finishes_when_it_answers(self):
        self.service.share_game_folder("test-mods")
        with patch.object(self.service, "ensure_running", side_effect=OSError("down")):
            self.assertFalse(self.service.stop_game_folder("test-mods"))
        self.assertTrue(folders.stopping("test-mods"))
        self.service.ensure_running()
        self.assertEqual(State.load().game_folders, {})

    def test_stopping_a_folder_syncthing_already_dropped(self):
        self.service.share_game_folder("test-mods")
        del self.st.folders["modsync-test-mods"]
        self.assertTrue(self.service.stop_game_folder("test-mods"))
        self.assertEqual(State.load().game_folders, {})

    def test_sharing_again_after_a_move_moves_it_safely(self):
        self.service.share_game_folder("test-mods")
        self.st.folders["modsync-test-mods"]["paused"] = True
        deleted = []
        original = _Client.delete_folder
        with patch.object(_Client, "delete_folder", lambda c, fid: (deleted.append(fid), original(c, fid))):
            self.root = self.tmp / "Elsewhere" / "Mods"
            self.service._folders_refreshed_at = 0.0
            with patch.object(self.service, "_FOLDERS_REFRESH_S", 1e9):  # the refresh hasn't come round yet
                self.service.share_game_folder("test-mods")
        self.assertEqual(deleted, ["modsync-test-mods"])
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.root))
        self.assertTrue(self.st.folders["modsync-test-mods"]["paused"])

    def test_a_moved_game_is_added_again_not_repointed(self):
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        self.service.share_game_folder("test-mods")
        deleted = []
        original = _Client.delete_folder
        with patch.object(_Client, "delete_folder", lambda c, fid: (deleted.append(fid), original(c, fid))):
            self.root = self.tmp / "Elsewhere" / "Mods"
            self.service.ensure_running()
        self.assertEqual(deleted, ["modsync-test-mods"])
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.root))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})
        self.assertTrue((self.root / ".stignore").exists())

    def test_a_moved_folder_keeps_its_settings(self):
        self.service.share_game_folder("test-mods")
        folder = self.st.folders["modsync-test-mods"]
        folder.update(paused=True, type="receiveonly", versioning={"type": "trashcan", "params": {}})
        self.root = self.tmp / "Elsewhere" / "Mods"
        self.service._refresh_game_folders(force=True)
        moved = self.st.folders["modsync-test-mods"]
        self.assertEqual((moved["path"], moved["paused"], moved["type"], moved["versioning"]["type"]),
                         (str(self.root), True, "receiveonly", "trashcan"))
        self.assertEqual(State.load().game_folders, {"test-mods": str(self.root)})


class MovedGameFailures(GameFolderBase):
    def setUp(self):
        super().setUp()
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        self.service.share_game_folder("test-mods")
        self.before = copy.deepcopy(self.st.folders["modsync-test-mods"])

    def test_a_place_that_cant_be_set_up_leaves_the_folder_alone(self):
        blocker = self.tmp / "file"
        blocker.write_text("not a directory")
        self.root = blocker / "Mods"
        self.service._refresh_game_folders(force=True)
        self.assertEqual(self.st.folders["modsync-test-mods"], self.before)
        self.assertNotEqual(State.load().game_folders["test-mods"], str(self.root))

    def test_a_failed_re_add_puts_the_old_folder_back(self):
        self.root = self.tmp / "Elsewhere"
        original = _Client.put_folder

        def put(client, folder):
            if folder["path"] == str(self.root):
                raise OSError("Syncthing said no")
            original(client, folder)

        with patch.object(_Client, "put_folder", put):
            self.service._refresh_game_folders(force=True)
        self.assertEqual(self.st.folders["modsync-test-mods"], self.before)
        self.assertNotEqual(State.load().game_folders["test-mods"], str(self.root))

    def test_a_game_that_moved_while_running_follows_once_it_closes(self):
        running = [True]
        self.spec = dataclasses.replace(self.spec, in_use=lambda: running[0])
        self.root = self.tmp / "Elsewhere" / "Mods"
        self.service._refresh_game_folders(force=True)
        self.assertEqual(self.st.folders["modsync-test-mods"], self.before)  # nothing copied into it meanwhile
        self.assertFalse(self.root.exists())
        running[0] = False
        self.service._refresh_game_folders(force=True)
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.root))

    def test_using_another_dfu_moves_its_mods_sync_straight_away(self):
        from modsync import dfu

        self.spec = dataclasses.replace(self.spec, key="dfu-mods")
        self.service.share_game_folder("dfu-mods")
        self.service._folders_refreshed_at = time.monotonic()  # refreshed just now: only forcing moves it

        def adopt(path, hook_error=None):
            self.root = Path(path)
            State.update(dfu_path=str(path))
            if hook_error:
                raise hook_error
            return "adopted", None

        with patch.object(dfu, "adopt", adopt), patch.object(dfu, "MODS_FOLDER", self.spec):
            self.assertEqual(self.service.use_dfu_install(self.tmp / "My DFU"), ("adopted", None))
            self.assertEqual(self.st.folders["modsync-dfu-mods"]["path"], str(self.tmp / "My DFU"))
            # Chosen, but Steam's Play button couldn't follow: mods sync still moves, and the error comes through.
            with patch.object(dfu, "adopt", lambda path: adopt(path, RuntimeError("Steam's config is read-only"))):
                with self.assertRaisesRegex(RuntimeError, "read-only"):
                    self.service.use_dfu_install(self.tmp / "Other DFU")
            self.assertEqual(self.st.folders["modsync-dfu-mods"]["path"], str(self.tmp / "Other DFU"))
            # Not chosen at all (say, no DFU there): nothing moves.
            with patch.object(dfu, "adopt", side_effect=dfu.DfuError("There's no Daggerfall Unity there.")):
                with self.assertRaises(dfu.DfuError):
                    self.service.use_dfu_install(self.tmp / "Nothing")
            self.assertEqual(self.st.folders["modsync-dfu-mods"]["path"], str(self.tmp / "Other DFU"))

    def test_refresh_runs_at_most_once_a_minute(self):
        calls = []
        with patch.object(folders, "refresh", lambda client: calls.append(1)):
            self.service._refresh_game_folders(force=True)
            self.service.ensure_running()
            self.service.ensure_running()
        self.assertEqual(len(calls), 1)


class MachineWidePairing(GameFolderBase):
    def test_a_machine_that_only_syncs_games_gives_a_machine_code(self):
        self.assertIsNone(self.service.my_pairing_code())
        self.service.share_game_folder("test-mods")
        code = self.service.my_pairing_code()
        self.assertEqual(code.folder_id, "")
        self.assertTrue(code.encode().startswith("MODSYNC2-"))
        self.assertEqual(PairingCode.decode(code.encode()), code)

    def test_accepting_a_machine_shares_every_game_folder_with_it(self):
        self.service.share_game_folder("test-mods")
        self.st.pending_devices["DECK"] = {"name": "Steam Deck"}
        self.assertEqual(self.service.accept_pending(), ["DECK"])
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})
        self.assertTrue(State.load().paired)

    def test_a_machine_sharing_nothing_accepts_no_one(self):
        State.update(paired=True)
        self.st.pending_devices["STRANGER"] = {}
        self.assertEqual(self.service.accept_pending(), [])
        self.assertNotIn("STRANGER", self.st.devices)

    def test_pairing_takes_up_offers_once_the_game_is_set_up(self):
        root, self.root = self.root, None  # the game isn't installed here yet
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.assertIn("DESKTOP", self.st.devices)
        self.assertTrue(State.load().paired)
        self.st.pending_folders["modsync-test-mods"] = {"offeredBy": {"DESKTOP": {"label": "Test mods"}}}
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)  # waits for the game
        self.root = root
        self.service.accept_pending()
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DESKTOP"})
        self.assertEqual(State.load().game_folders, {"test-mods": str(root)})

    def test_offers_of_a_stopped_folder_or_from_strangers_are_left_alone(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.service.share_game_folder("test-mods")
        self.service.stop_game_folder("test-mods")
        self.st.pending_folders["modsync-test-mods"] = {"offeredBy": {"DESKTOP": {}}}
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)
        State.update(declined_folders=[])
        self.st.pending_folders["modsync-test-mods"] = {"offeredBy": {"STRANGER": {}}}
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.st.pending_folders["modsync-unknown"] = {"offeredBy": {"DESKTOP": {}}}
        self.service.accept_pending()
        self.assertNotIn("modsync-unknown", self.st.folders)

    def test_pairing_shares_existing_folders(self):
        self.service.share_game_folder("test-mods")
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DESKTOP"})

    def test_sharing_again_keeps_the_folders_settings_and_machines(self):
        self.service.share_game_folder("test-mods")
        self.st.folders["modsync-test-mods"]["paused"] = True
        self.st.folders["modsync-test-mods"]["devices"].append({"deviceID": "OLD"})
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        self.service.share_game_folder("test-mods")
        folder = self.st.folders["modsync-test-mods"]
        self.assertTrue(folder["paused"])
        self.assertEqual([d["deviceID"] for d in folder["devices"]], ["ME", "OLD", "DECK"])

    def test_a_machine_code_cannot_join_the_mo2_vault(self):
        with self.assertRaisesRegex(RuntimeError, "not Mod Organizer 2"):
            self.service.join_vault(PairingCode("DESKTOP", "", "ModSync"), self.tmp / "MO2")
        self.assertIsNone(State.load().folder_id)

    def test_lan_join_pairs_only_when_asked(self):
        from modsync import pairing_lan

        host = pairing_lan.PairPayload("DESKTOP", "", "ModSync", host="192.0.2.5")
        ann = pairing_lan.Announcement("desktop", "192.0.2.5", 21029, "s")
        with patch.object(pairing_lan, "join_pairing", return_value=host):
            with self.assertRaisesRegex(RuntimeError, "isn't offering a vault"):
                self.service.join_via_network(ann, "1234", self.tmp / "MO2")
            self.assertNotIn("DESKTOP", self.st.devices)
            self.service.join_via_network(ann, "1234", self.tmp / "MO2", pair_only=True)
        self.assertIn("DESKTOP", self.st.devices)
        self.assertIsNone(State.load().folder_id)

    def test_forgetting_mo2_keeps_the_other_games(self):
        self.service.share_game_folder("test-mods")
        State.update(instance_path=str(self.tmp / "MO2"), dfu_path="/games/dfu", paired=True,
                     declined_folders=["other"])
        self.service.reload_state()
        self.service.forget_instance()
        st = State.load()
        self.assertIsNone(st.instance_path)
        self.assertEqual((st.game_folders, st.dfu_path, st.paired, st.declined_folders),
                         ({"test-mods": str(self.root)}, "/games/dfu", True, ["other"]))

    def test_add_peer_works_without_a_vault(self):
        with self.assertRaisesRegex(RuntimeError, "no vault"):
            self.service.add_peer(PairingCode("DECK", "", "Deck"))
        self.service.share_game_folder("test-mods")
        self.service.add_peer(PairingCode("DECK", "", "Deck"))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})

    def test_a_new_vault_is_shared_with_machines_already_paired(self):
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        instance = self.tmp / "MO2"
        instance.mkdir()
        with patch.object(ModSyncService, "record_initial_vault_version"):
            self.service.create_vault(instance, "MO2")
        self.assertEqual(folder_devices(self.st, State.load().folder_id), {"ME", "DECK"})

    def test_leaving_the_vault_keeps_machines_the_games_still_sync_with(self):
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        self.service.share_game_folder("test-mods")
        State.update(instance_path=str(self.tmp / "MO2"), folder_id="modsync-abc", paired=True)
        self.service.stop_sync(forget_devices=True)
        self.assertIn("DECK", self.st.devices)
        self.assertTrue(State.load().paired)
        self.service.stop_game_folder("test-mods")
        State.update(instance_path=str(self.tmp / "MO2"), folder_id="modsync-abc")
        self.service.stop_sync(forget_devices=True)
        self.assertNotIn("DECK", self.st.devices)
        self.assertFalse(State.load().paired)

    def test_every_way_of_pairing_makes_the_other_machine_an_introducer(self):
        self.service.share_game_folder("test-mods")
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.service.add_peer(PairingCode("DECK", "", "Deck"))
        self.st.pending_devices["FRAME"] = {"name": "Steam Frame"}
        self.service.accept_pending()
        for device_id in ("DESKTOP", "DECK", "FRAME"):
            device = self.st.devices[device_id]
            self.assertTrue(device["introducer"], device_id)
            self.assertTrue(device["skipIntroductionRemovals"], device_id)

    def test_a_paired_machine_is_not_named_after_the_vault(self):
        # A code carries the vault's label: Syncthing names the machine as it
        # connects, from the name the machine gives itself.
        self.service.share_game_folder("test-mods")
        self.service.pair(PairingCode("DESKTOP", "", "Skyrim SE"))
        self.service.add_peer(PairingCode("DECK", "", "Skyrim SE"), peer_name="steamdeck")
        self.st.pending_devices["FRAME"] = {"name": "steamframe"}
        self.st.pending_devices["NONAME"] = {}
        self.service.accept_pending()
        self.assertEqual({d: self.st.devices[d]["name"] for d in ("DESKTOP", "DECK", "FRAME", "NONAME")},
                         {"DESKTOP": "", "DECK": "steamdeck", "FRAME": "steamframe", "NONAME": ""})

    def test_lan_pairing_names_each_machine_after_itself(self):
        from modsync import pairing_lan

        self.service.share_game_folder("test-mods")
        joiner = pairing_lan.PairPayload("LAPTOP", name="laptop")
        with patch.object(pairing_lan, "host_pairing", return_value=joiner) as host:
            self.service.host_network_pairing("desktop", "123456")
        self.assertEqual(host.call_args.args[0].name, "desktop")  # the host says who it is too
        self.assertEqual(self.st.devices["LAPTOP"]["name"], "laptop")  # not "desktop"

        old_host = pairing_lan.PairPayload("DECK", "", "Skyrim SE")  # an older version sends no name
        ann = pairing_lan.Announcement("steamdeck", "10.0.0.2", 21029, "s")
        with patch.object(pairing_lan, "join_pairing", return_value=old_host) as join:
            self.service.join_via_network(ann, "123456", self.tmp / "MO2", pair_only=True)
        self.assertTrue(join.call_args.args[1].name)  # this machine's hostname
        self.assertEqual(self.st.devices["DECK"]["name"], "steamdeck")  # from its announcement

    def test_machines_paired_before_become_introducers_once(self):
        self.st.devices["DESKTOP"] = {"deviceID": "DESKTOP", "name": "Desktop", "introducer": False,
                                      "paused": True}
        self.service.ensure_running()  # nothing syncs or is paired here yet: left alone
        self.assertFalse(self.st.devices["DESKTOP"]["introducer"])
        State.update(paired=True)
        self.service.reload_state()
        self.service.ensure_running()
        desktop = self.st.devices["DESKTOP"]
        self.assertEqual((desktop["introducer"], desktop["skipIntroductionRemovals"], desktop["paused"]),
                         (True, True, True))
        self.assertNotIn("introducer", self.st.devices["ME"])
        self.st.devices["DESKTOP"]["introducer"] = False
        self.service.ensure_running()  # once per run
        self.assertFalse(self.st.devices["DESKTOP"]["introducer"])

    def test_a_connected_machine_made_an_introducer_reconnects(self):
        self.st.devices["DECK"] = {"deviceID": "DECK", "paused": False}
        self.st.devices["PAUSED"] = {"deviceID": "PAUSED", "paused": True}
        written = []
        put = _Client.put_device

        def record(client, device):
            written.append((device["deviceID"], device.get("paused")))
            put(client, device)

        State.update(paired=True)
        self.service.reload_state()
        with patch.object(_Client, "put_device", record):
            self.service.ensure_running()
        # Syncthing reads what an introducer passes on only as it connects.
        self.assertEqual(written, [("DECK", True), ("DECK", False), ("PAUSED", True)])

    def test_a_machine_introduced_by_another_gets_the_games_folders(self):
        self.service.share_game_folder("test-mods")
        self.st.devices["LAPTOP"] = {"deviceID": "LAPTOP", "introducer": True}  # Syncthing added it
        self.service.accept_pending()
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "LAPTOP"})

    def test_status_lists_the_games_folders(self):
        self.service.share_game_folder("test-mods")
        st = self.service.status()
        self.assertEqual([(f.spec.key, f.state, f.completion) for f in st.game_folders], [("test-mods", "idle", 100)])


class Cli(GameFolderBase):
    def _run(self, *args):
        import io
        from contextlib import redirect_stdout

        from modsync import cli

        out = io.StringIO()
        with redirect_stdout(out), patch("modsync.service.ModSyncService", lambda: self.service):
            code = cli.vault(list(args))
        return code, out.getvalue()

    def test_share_code_and_stop(self):
        code, out = self._run("share", "test-mods")
        self.assertEqual(code, 0, out)
        self.assertIn("No machine is paired yet", out)
        code, out = self._run("code")
        self.assertTrue(out.startswith("MODSYNC2-"), out)
        self.assertEqual(self._run("stop", "test-mods")[0], 0)
        code, out = self._run("share", "nope")
        self.assertEqual(code, 2)
        self.assertIn("known: test-mods", out)

    def test_join_with_a_machine_code_that_fails_says_so(self):
        text = PairingCode("DESKTOP", "", "ModSync").encode()
        with patch.object(self.service, "pair", side_effect=RuntimeError("Syncthing did not start")):
            code, out = self._run("join", text)
        self.assertEqual(code, 1)
        self.assertIn("Couldn't pair: Syncthing did not start", out)

    def test_a_vault_code_still_needs_the_instance(self):
        code, out = self._run("join", PairingCode("DESKTOP", "modsync-1", "MO2").encode())
        self.assertEqual(code, 2)
        self.assertIn("<instance-dir>", out)


class StateFile(unittest.TestCase):
    def test_damaged_folder_fields_do_not_take_the_rest_with_them(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            State.path().parent.mkdir(parents=True)
            State.path().write_text('{"instance_path": "/mo2", "game_folders": null, "declined_folders": "x"}')
            st = State.load()
            self.assertEqual(st.instance_path, "/mo2")
            self.assertEqual(st.game_folders, {})
            self.assertEqual(st.declined_folders, [])

    def test_older_state_files_load_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            State.path().parent.mkdir(parents=True)
            State.path().write_text('{"instance_path": "/mo2", "folder_id": "modsync-1"}')
            st = State.load()
            self.assertTrue(st.syncing and st.active)
            self.assertFalse(st.paired)
            self.assertEqual(st.game_folders, {})


class PairingCodes(unittest.TestCase):
    def test_vault_codes_keep_the_old_prefix(self):
        code = PairingCode("DEV", "modsync-1", "MO2", 4)
        self.assertTrue(code.encode().startswith("MODSYNC1-"))
        self.assertTrue(code.has_vault)
        self.assertEqual(PairingCode.decode(code.encode()), code)

    def test_older_versions_reject_machine_codes(self):
        import base64
        import json

        text = PairingCode("DEV", "", "ModSync").encode()
        body = text.removeprefix("MODSYNC1-")  # what an older version strips: nothing
        with self.assertRaises(Exception):
            json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
