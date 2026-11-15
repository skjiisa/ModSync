"""The games' folders and machine-wide pairing, against an in-memory Syncthing."""

import copy
import dataclasses
import os
import tempfile
import time
import unittest
import unittest.mock
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
        self.stats: dict | None = None  # /rest/stats/device; None: Syncthing doesn't answer it
        self.connected: set[str] = set()

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
        return {"connections": {d: {"connected": True} for d in self.st.connected}}

    def device_stats(self):
        if self.st.stats is None:
            raise OSError("no stats")
        return copy.deepcopy(self.st.stats)

    def dismiss_pending_device(self, device_id):
        self.st.pending_devices.pop(device_id, None)


def folder_devices(st: FakeSyncthing, folder_id: str) -> set[str]:
    return {d["deviceID"] for d in st.folders[folder_id]["devices"]}


class GameFolderBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        data = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.tmp / "data")})  # where kept copies go
        data.start()
        self.addCleanup(data.stop)
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

    def test_an_offer_waits_for_share_where_the_spec_refuses_it(self):
        """A folder with content of its own (Cyberpunk: installed mods) isn't
        filled by an offer; 'share' is the choice to merge."""
        import dataclasses

        self.spec = dataclasses.replace(self.spec, accepts=lambda root: False)
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.st.pending_folders["modsync-test-mods"] = {"offeredBy": {"DESKTOP": {"label": "Test mods"}}}
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.service.share_game_folder("test-mods")
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DESKTOP"})

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

    def test_lan_join_without_mo2_pairs_with_a_vault_host(self):
        from modsync import pairing_lan

        host = pairing_lan.PairPayload("DESKTOP", "modsync-vault", "Skyrim SE", host="192.0.2.5", name="desktop")
        ann = pairing_lan.Announcement("desktop", "192.0.2.5", 21029, "s")
        with patch.object(pairing_lan, "join_pairing", return_value=host), \
                patch.object(self.service, "join_vault") as join:
            self.service.join_via_network(ann, "1234", None)
        join.assert_not_called()  # no Mod Organizer 2 here to copy it into
        self.assertIn("DESKTOP", self.st.devices)
        st = State.load()
        self.assertIsNone(st.folder_id)
        self.assertTrue(st.paired)

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

    def test_the_machine_entering_a_code_takes_introductions_from_the_other(self):
        from modsync import pairing_lan

        self.service.share_game_folder("test-mods")
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.service.add_peer(PairingCode("DECK", "", "Deck"))
        self.st.pending_devices["FRAME"] = {"name": "Steam Frame"}  # it entered this machine's code
        self.service.accept_pending()
        with patch.object(pairing_lan, "host_pairing", return_value=pairing_lan.PairPayload("LAPTOP")):
            self.service.host_network_pairing("me", "123456")  # LAPTOP entered the PIN shown here
        flags = {d: (self.st.devices[d]["introducer"], self.st.devices[d]["skipIntroductionRemovals"])
                 for d in ("DESKTOP", "DECK", "FRAME", "LAPTOP")}
        self.assertEqual(flags, {"DESKTOP": (True, True), "DECK": (True, True),
                                 "FRAME": (False, False), "LAPTOP": (False, False)})

    def test_pairings_from_before_introduce_one_way_once(self):
        # Before, both machines of a pair introduced each other (or, before
        # that, neither did). Both apply the same rule: the lower id introduces.
        self.st.devices["AAA"] = {"deviceID": "AAA", "introducer": True, "skipIntroductionRemovals": True,
                                  "paused": True}
        self.st.devices["ZZZ"] = {"deviceID": "ZZZ", "introducer": True, "skipIntroductionRemovals": True}
        self.st.devices["BBB"] = {"deviceID": "BBB"}
        self.service.ensure_running()
        flags = {d: (self.st.devices[d].get("introducer"), self.st.devices[d].get("skipIntroductionRemovals"))
                 for d in ("AAA", "BBB", "ZZZ")}
        self.assertEqual(flags, {"AAA": (True, True), "BBB": (True, True), "ZZZ": (False, False)})
        self.assertTrue(self.st.devices["AAA"]["paused"])  # the user paused it
        self.assertNotIn("introducer", self.st.devices["ME"])
        self.assertTrue(State.load().one_way_introducers)
        # Once: a pairing made afterwards keeps the way it was made.
        self.st.devices["ZZZ"]["introducer"] = True
        ModSyncService(manager=self.st).ensure_running()
        self.assertTrue(self.st.devices["ZZZ"]["introducer"])

    def test_settling_never_turns_around_a_pairing_made_since(self):
        # The app and the background service each have a service; the one that
        # still thinks nothing was settled must not settle a new pairing.
        stale = ModSyncService(manager=self.st)
        self.service.ensure_running()
        self.service.pair(PairingCode("ZZZ", "", ""))  # sorts after ME, entered here: introducer
        self.assertFalse(stale.state.one_way_introducers)
        stale.ensure_running()
        self.assertTrue(self.st.devices["ZZZ"]["introducer"])

    def test_pairing_waits_for_settling(self):
        from modsync.sync import pairing

        with patch.object(pairing, "introduce_one_way", side_effect=OSError("Syncthing went away")):
            with self.assertRaisesRegex(RuntimeError, "getting ready to pair"):
                self.service.pair(PairingCode("ZZZ", "", ""))
        self.assertNotIn("ZZZ", self.st.devices)
        self.assertFalse(State.load().one_way_introducers)

    def test_a_connected_machine_made_an_introducer_reconnects(self):
        self.st.devices["DECK"] = {"deviceID": "DECK", "paused": False}
        self.st.devices["CPAUSED"] = {"deviceID": "CPAUSED", "paused": True}
        self.st.devices["ZZZ"] = {"deviceID": "ZZZ", "paused": False, "introducer": True}
        written = []
        put = _Client.put_device

        def record(client, device):
            written.append((device["deviceID"], device.get("paused"), device.get("introducer")))
            put(client, device)

        with patch.object(_Client, "put_device", record):
            self.service.ensure_running()
        # Syncthing reads what an introducer passes on only as it connects; one
        # that stops being an introducer needn't reconnect.
        self.assertEqual(written, [("DECK", True, True), ("DECK", False, True), ("CPAUSED", True, True),
                                   ("ZZZ", False, False)])

    def test_a_machine_introduced_by_another_gets_the_games_folders(self):
        self.service.share_game_folder("test-mods")
        self.st.devices["LAPTOP"] = {"deviceID": "LAPTOP", "introducer": True}  # Syncthing added it
        self.service.accept_pending()
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "LAPTOP"})

    def test_an_introduced_machine_gets_the_games_folders_once_it_has_connected(self):
        self.service.share_game_folder("test-mods")
        never = {"lastSeen": "1970-01-01T00:00:00Z"}
        self.st.stats = {"LAPTOP": never, "OLD": never, "FRAME": {"lastSeen": "2026-11-01T10:00:00Z"}}
        for did in ("LAPTOP", "OLD", "FRAME"):
            self.st.devices[did] = {"deviceID": did}  # another machine passed them on
        self.service.accept_pending()
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "FRAME"})
        self.st.connected.add("LAPTOP")
        self.service.accept_pending()
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "FRAME", "LAPTOP"})

    def test_a_game_folder_taken_up_skips_machines_that_never_connected(self):
        self.st.stats = {"OLD": {"lastSeen": "1970-01-01T00:00:00Z"}}
        self.st.devices["OLD"] = {"deviceID": "OLD"}
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        self.st.connected.add("DECK")
        self.st.pending_folders["modsync-test-mods"] = {"offeredBy": {"DECK": {}}}
        State.update(paired=True)
        self.service.reload_state()
        self.service.accept_pending()
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})


class ForgetMachine(GameFolderBase):
    def setUp(self):
        super().setUp()
        self.service.share_game_folder("test-mods")
        self.service.pair(PairingCode("DECK", "", ""))
        self.service.pair(PairingCode("OLD", "", ""))
        self.st.devices["OLD"]["name"] = "modorganizer2"

    def test_forgetting_removes_it_everywhere_here(self):
        self.service.forget_machine("OLD")
        self.assertNotIn("OLD", self.st.devices)
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})
        self.assertEqual(State.load().forgotten_devices, ["OLD"])
        self.assertTrue(State.load().paired)  # DECK is still paired
        self.service.forget_machine("DECK")
        self.assertFalse(State.load().paired)

    def test_a_forgotten_machine_passed_on_again_goes_again(self):
        self.service.forget_machine("OLD")
        self.st.devices["OLD"] = {"deviceID": "OLD", "introducedBy": "DECK"}  # the Deck still lists it
        self.st.folders["modsync-test-mods"]["devices"].append({"deviceID": "OLD"})
        self.st.connected.add("OLD")
        self.service.accept_pending()
        self.assertNotIn("OLD", self.st.devices)
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})

    def test_any_call_to_syncthing_drops_a_forgotten_machine_that_is_back(self):
        self.service.forget_machine("OLD")
        self.st.devices["OLD"] = {"deviceID": "OLD", "introducedBy": "DECK"}
        self.service.status()
        self.assertNotIn("OLD", self.st.devices)

    def test_pairing_again_while_the_background_service_looks(self):
        from modsync.sync import pairing

        self.service.forget_machine("OLD")
        background = ModSyncService(manager=self.st)
        background.reload_state()  # still lists OLD as forgotten
        add = pairing.add_peer_device

        def add_then_background_runs(*args, **kwargs):
            device = add(*args, **kwargs)
            background._drop_forgotten()
            return device

        with patch.object(pairing, "add_peer_device", add_then_background_runs):
            self.service.pair(PairingCode("OLD", "", ""))
        self.assertIn("OLD", self.st.devices)
        self.assertEqual(State.load().forgotten_devices, [])

    def test_a_forgotten_machine_knocking_is_not_accepted(self):
        self.service.forget_machine("OLD")
        self.st.pending_devices["OLD"] = {"name": "modorganizer2"}
        self.assertEqual(self.service.accept_pending(), [])
        self.assertNotIn("OLD", self.st.devices)

    def test_entering_its_code_again_pairs_it_again(self):
        self.service.forget_machine("OLD")
        self.service.pair(PairingCode("OLD", "", ""))
        self.assertIn("OLD", self.st.devices)
        self.assertEqual(State.load().forgotten_devices, [])

    def test_this_machine_and_the_copy_source_cant_be_forgotten(self):
        with self.assertRaisesRegex(RuntimeError, "this machine"):
            self.service.forget_machine("ME")
        State.update(instance_path=str(self.tmp / "MO2"), folder_id="modsync-abc", copy_phase="receiving",
                     copy_source="DECK")
        with self.assertRaisesRegex(RuntimeError, "still copying"):
            self.service.forget_machine("DECK")
        self.assertIn("DECK", self.st.devices)

    def test_finding_a_machine(self):
        self.st.devices["DECK2"] = {"deviceID": "DECKDEC-ABCDEFG", "name": "steamdeck"}
        self.st.devices["DECKDEC-ABCDEFG"] = self.st.devices.pop("DECK2")
        self.assertEqual(self.service.find_machine("modorganizer2"), "OLD")
        self.assertEqual(self.service.find_machine("SteamDeck"), "DECKDEC-ABCDEFG")
        self.assertEqual(self.service.find_machine("deckdec-a"), "DECKDEC-ABCDEFG")
        with self.assertRaisesRegex(RuntimeError, "No paired machine"):
            self.service.find_machine("laptop")
        with self.assertRaisesRegex(RuntimeError, "No paired machine"):
            self.service.find_machine("ME")  # not this machine

    def test_status_lists_the_games_folders(self):
        self.service.share_game_folder("test-mods")
        st = self.service.status()
        self.assertEqual([(f.spec.key, f.state, f.completion) for f in st.game_folders], [("test-mods", "idle", 100)])



class KeptCopies(GameFolderBase):
    """A copy of a game's folder kept for the other machines, the game not
    being set up here."""

    def setUp(self):
        super().setUp()
        self.game_root, self.root = self.root, None  # the game isn't installed here
        self.kept = folders.held_root(self.spec)

    def offer(self, by="DESKTOP"):
        self.st.pending_folders["modsync-test-mods"] = {"offeredBy": {by: {"label": "Test mods"}}}

    def test_a_kept_copy_is_receive_only_and_shared_with_every_paired_machine(self):
        self.st.devices["DECK"] = {"deviceID": "DECK"}
        root = self.service.hold_game_folder("test-mods")
        self.assertEqual(root, self.kept)
        self.assertTrue(str(root).endswith("held/modsync-test-mods"))
        folder = self.st.folders["modsync-test-mods"]
        self.assertEqual((folder["path"], folder["type"]), (str(root), "receiveonly"))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})
        self.assertEqual((root / ".stignore").read_text(), "// test\n!/keep\n/*\n")
        st = State.load()
        self.assertEqual((st.held_folders, st.game_folders), ({"test-mods": str(root)}, {}))
        self.assertTrue(st.syncs_anything)
        self.assertTrue(self.service.my_pairing_code().encode().startswith("MODSYNC2-"))

    def test_a_game_set_up_here_syncs_where_it_is_instead(self):
        self.root = self.game_root
        with self.assertRaisesRegex(RuntimeError, "modsync sync share test-mods"):
            self.service.hold_game_folder("test-mods")
        self.service.share_game_folder("test-mods")
        with self.assertRaisesRegex(RuntimeError, "already syncs here"):
            self.service.hold_game_folder("test-mods")
        self.assertEqual(State.load().held_folders, {})

    def test_offers_are_kept_only_once_asked(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.offer()
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.assertEqual(self.service.hold_all_games(True), ["test-mods"])
        folder = self.st.folders["modsync-test-mods"]
        self.assertEqual((folder["path"], folder["type"]), (str(self.kept), "receiveonly"))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DESKTOP"})
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})

    def test_with_every_game_kept_a_later_offer_is_taken_up_by_itself(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.assertEqual(self.service.hold_all_games(True), [])
        self.offer()
        self.service.accept_pending()
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})

    def test_turned_off_new_offers_wait_and_kept_copies_stay(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.offer()
        self.service.hold_all_games(True)
        self.service.hold_all_games(False)
        self.assertFalse(State.load().hold_all)
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})
        self.assertIn("modsync-test-mods", self.st.folders)

    def test_a_game_set_up_here_is_taken_up_at_its_own_place_even_when_keeping_every_game(self):
        self.root = self.game_root
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.offer()
        self.service.hold_all_games(True)
        folder = self.st.folders["modsync-test-mods"]
        self.assertEqual((folder["path"], folder["type"]), (str(self.game_root), "sendreceive"))
        self.assertEqual(State.load().game_folders, {"test-mods": str(self.game_root)})
        self.assertEqual(State.load().held_folders, {})

    def test_offers_from_strangers_are_not_kept(self):
        State.update(paired=True)
        self.service.hold_all_games(True)
        self.offer(by="STRANGER")
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)

    def test_the_games_own_hooks_leave_a_kept_copy_alone(self):
        hooks = {name: unittest.mock.Mock(return_value=True) for name in ("on_share", "in_use", "tidy", "accepts")}
        hooks["on_stop"] = unittest.mock.Mock()
        self.spec = dataclasses.replace(self.spec, **hooks)
        self.service.hold_game_folder("test-mods")
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        [fs] = self.service.status().game_folders
        self.assertTrue(fs.held)
        self.service._refresh_game_folders(force=True)
        self.service.stop_game_folder("test-mods")
        for name, hook in hooks.items():
            hook.assert_not_called()

    def test_a_kept_copy_counts_no_conflicts(self):
        self.service.hold_game_folder("test-mods")
        (self.kept / "keep").mkdir()
        (self.kept / "keep" / "a.sync-conflict-20261114-120000-ABCDEFG.txt").write_text("x")
        self.spec = dataclasses.replace(self.spec, content=("keep",))
        [fs] = self.service.status().game_folders
        self.assertEqual((fs.held, fs.conflicts), (True, 0))

    def test_a_newly_paired_machine_gets_the_kept_copies(self):
        self.service.hold_game_folder("test-mods")
        self.st.pending_devices["DECK"] = {"name": "deck"}
        self.assertEqual(self.service.accept_pending(), ["DECK"])
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DECK"})

    def test_stopping_keeps_the_files_and_the_copy_isnt_taken_up_again(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.offer()
        self.service.hold_all_games(True)
        (self.kept / "keep").write_text("theirs")
        self.assertTrue(self.service.stop_game_folder("test-mods"))
        self.assertEqual((self.kept / "keep").read_text(), "theirs")
        st = State.load()
        self.assertEqual((st.held_folders, st.declined_folders), ({}, ["test-mods"]))
        self.offer()
        self.service.accept_pending()
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.service.hold_all_games(False)
        self.assertEqual(self.service.hold_all_games(True), ["test-mods"])  # off and on keeps it again
        self.assertEqual(State.load().declined_folders, [])

    def test_turning_it_on_leaves_a_game_stopped_where_it_is_set_up_stopped(self):
        self.root = self.game_root
        self.service.share_game_folder("test-mods")
        self.service.stop_game_folder("test-mods")
        self.service.hold_all_games(True)
        self.assertEqual(State.load().declined_folders, ["test-mods"])

    def test_a_stop_syncthing_refuses_finishes_later(self):
        self.service.hold_game_folder("test-mods")
        self.st.refuse_delete = True
        self.assertFalse(self.service.stop_game_folder("test-mods"))
        self.assertTrue(folders.stopping("test-mods"))
        self.st.refuse_delete = False
        self.service._refresh_game_folders(force=True)
        self.assertNotIn("modsync-test-mods", self.st.folders)
        self.assertEqual(State.load().held_folders, {})

    def test_a_retried_stop_leaves_a_folder_taken_up_again_alone(self):
        """The background service retries a refused stop while the app shares
        the folder: the retry checks again under the state's lock."""
        self.service.hold_game_folder("test-mods")
        self.st.refuse_delete = True
        self.service.stop_game_folder("test-mods")
        self.st.refuse_delete = False
        self.root = self.game_root
        self.service.share_game_folder("test-mods")  # takes it up again: no longer stopping
        folders._retry_stop(_Client(self.st), self.spec)
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.game_root))
        self.assertEqual(State.load().game_folders, {"test-mods": str(self.game_root)})

    def test_a_stop_finishing_after_a_share_forgets_nothing_and_runs_no_on_stop(self):
        self.spec = dataclasses.replace(self.spec, on_stop=unittest.mock.Mock())
        State.update(game_folders={"test-mods": str(self.game_root)})  # shared again meanwhile
        folders._finish_stop(self.spec)
        self.assertEqual(State.load().game_folders, {"test-mods": str(self.game_root)})
        # Half-way: the share has let go of the kept copy but not recorded its own yet.
        State.update(game_folders={}, declined_folders=["test-mods"])
        folders._finish_stop(self.spec)
        self.spec.on_stop.assert_not_called()  # the game never used a kept copy

    def test_a_share_that_fails_keeps_the_copy_as_it_was(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.service.hold_game_folder("test-mods")
        self.root = self.game_root
        with patch.object(folders, "_put", side_effect=OSError("Syncthing said no")), \
                self.assertRaisesRegex(OSError, "said no"):
            self.service.share_game_folder("test-mods")
        folder = self.st.folders["modsync-test-mods"]
        self.assertEqual((folder["path"], folder["type"]), (str(self.kept), "receiveonly"))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DESKTOP"})
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})

    def test_a_kept_copy_stays_put_when_the_game_is_set_up_later(self):
        self.service.hold_game_folder("test-mods")
        self.root = self.game_root
        self.service._refresh_game_folders(force=True)
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.kept))
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})

    def test_sharing_once_the_game_is_set_up_syncs_there_instead(self):
        self.spec = dataclasses.replace(self.spec, on_share=unittest.mock.Mock())
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.service.hold_game_folder("test-mods")
        (self.kept / "keep").write_text("theirs")
        self.root = self.game_root
        self.assertEqual(self.service.share_game_folder("test-mods"), self.game_root)
        folder = self.st.folders["modsync-test-mods"]
        self.assertEqual((folder["path"], folder["type"]), (str(self.game_root), "sendreceive"))
        self.assertEqual(folder_devices(self.st, "modsync-test-mods"), {"ME", "DESKTOP"})
        st = State.load()
        self.assertEqual((st.game_folders, st.held_folders), ({"test-mods": str(self.game_root)}, {}))
        self.assertEqual((self.kept / "keep").read_text(), "theirs")  # left where it was
        self.spec.on_share.assert_not_called()  # the folder exists elsewhere: its record comes from there

    def use_mods_spec(self, accepts=lambda root: not any((root / "mods").glob("*"))):
        """A game whose folder syncs mods/ and profiles/, and fills by itself
        only while it has no mods, like an MO2 instance (mo2games)."""
        self.spec = dataclasses.replace(self.spec, content=("mods", "profiles", "record.json"), accepts=accepts)
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.st.connected = {"DESKTOP"}  # the copy is current: a machine it's shared with is here
        self.service.hold_game_folder("test-mods")
        for rel, text in (("mods/A Mod/plugin.esp", "esp"), ("profiles/Default/modlist.txt", "+A Mod\n"),
                          ("record.json", "{}")):
            (self.kept / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.kept / rel).write_text(text)
        os.utime(self.kept / "mods/A Mod/plugin.esp", (1_700_000_000, 1_700_000_000))
        self.root = self.game_root

    def test_a_new_instance_takes_the_kept_copys_files_instead_of_downloading_them(self):
        self.use_mods_spec()
        (self.game_root / "profiles/Default").mkdir(parents=True)  # what a new MO2 makes
        (self.game_root / "profiles/Default/modlist.txt").write_text("# new\n")
        self.service.share_game_folder("test-mods")
        self.assertEqual((self.game_root / "mods/A Mod/plugin.esp").read_text(), "esp")
        self.assertEqual((self.game_root / "mods/A Mod/plugin.esp").stat().st_mtime, 1_700_000_000)  # kept as is
        self.assertEqual((self.game_root / "profiles/Default/modlist.txt").read_text(), "+A Mod\n")
        self.assertEqual((self.game_root / "record.json").read_text(), "{}")
        replaced = list((self.kept / folders.REPLACED_DIR).glob("*/profiles/Default/modlist.txt"))
        self.assertEqual([p.read_text() for p in replaced], ["# new\n"])  # set aside, not deleted
        self.assertFalse((self.kept / "mods").exists())
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], str(self.game_root))
        self.assertEqual(State.load().held_folders, {})

    def test_an_emptied_kept_copy_is_removed(self):
        self.use_mods_spec()
        self.service.share_game_folder("test-mods")
        self.assertFalse(self.kept.exists())

    def test_an_instance_with_mods_of_its_own_leaves_the_kept_copy_alone(self):
        self.use_mods_spec()
        (self.game_root / "mods/My Mod").mkdir(parents=True)
        self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / "mods/A Mod/plugin.esp").read_text(), "esp")
        self.assertFalse((self.game_root / "mods/A Mod").exists())

    def test_a_share_that_fails_puts_the_files_back_and_keeps_the_copy(self):
        self.use_mods_spec()
        (self.game_root / "profiles/Default").mkdir(parents=True)
        (self.game_root / "profiles/Default/modlist.txt").write_text("# new\n")
        before = self.st.folders["modsync-test-mods"]
        with patch.object(folders, "_put", side_effect=RuntimeError("Syncthing said no")), \
                self.assertRaisesRegex(RuntimeError, "Syncthing said no"):
            self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / "mods/A Mod/plugin.esp").read_text(), "esp")
        self.assertEqual((self.kept / "profiles/Default/modlist.txt").read_text(), "+A Mod\n")
        self.assertEqual((self.game_root / "profiles/Default/modlist.txt").read_text(), "# new\n")
        self.assertFalse((self.game_root / "mods").exists())
        self.assertEqual(self.st.folders["modsync-test-mods"]["path"], before["path"])
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})

    def test_without_a_rule_of_its_own_any_file_counts_as_content(self):
        self.use_mods_spec(accepts=None)
        (self.game_root / "profiles").mkdir(parents=True)
        (self.game_root / "profiles/mine.txt").write_text("mine")
        self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / "mods/A Mod/plugin.esp").read_text(), "esp")  # nothing moved

    def test_a_copy_that_may_be_behind_isnt_moved_in(self):
        """It may have missed a deletion on another machine, and would bring it back."""
        self.use_mods_spec()
        self.st.connected = set()
        self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / "mods/A Mod/plugin.esp").read_text(), "esp")
        self.assertFalse((self.game_root / "mods").exists())

    def test_a_copy_is_current_only_with_nothing_to_receive_and_a_machine_connected(self):
        client = self.st.client().__enter__()
        folder = {"devices": [{"deviceID": "ME"}, {"deviceID": "DESKTOP"}]}
        self.st.connected = {"DESKTOP"}
        self.assertTrue(folders._kept_is_current(client, self.spec, folder))
        self.assertFalse(folders._kept_is_current(client, self.spec, None))
        with patch.object(type(client), "folder_status", lambda *_: {"state": "idle", "needTotalItems": 3}):
            self.assertFalse(folders._kept_is_current(client, self.spec, folder))
        with patch.object(type(client), "folder_status", lambda *_: {"state": "syncing", "needTotalItems": 0}):
            self.assertFalse(folders._kept_is_current(client, self.spec, folder))
        with patch.object(type(client), "folder_status", side_effect=RuntimeError("no answer")):
            self.assertFalse(folders._kept_is_current(client, self.spec, folder))
        self.st.connected = {"ME"}  # only this machine: nobody to be current with
        self.assertFalse(folders._kept_is_current(client, self.spec, folder))

    def test_on_another_disk_nothing_moves(self):
        """A move there is a copy that can stop half way; the files download instead."""
        self.use_mods_spec()
        with patch.object(folders, "_same_disk", return_value=False):
            self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / "mods/A Mod/plugin.esp").read_text(), "esp")
        self.assertFalse((self.game_root / "mods").exists())

    def test_a_rename_that_fails_half_way_puts_everything_back(self):
        self.use_mods_spec()
        (self.game_root / "profiles/Default").mkdir(parents=True)
        (self.game_root / "profiles/Default/modlist.txt").write_text("# new\n")
        real = os.rename

        def rename(src, dst):
            if Path(src) == self.kept / "profiles":
                raise OSError(28, "No space left on device")
            return real(src, dst)

        with patch.object(folders.os, "rename", side_effect=rename), \
                self.assertRaisesRegex(OSError, "No space"):
            self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / "mods/A Mod/plugin.esp").read_text(), "esp")
        self.assertEqual((self.kept / "profiles/Default/modlist.txt").read_text(), "+A Mod\n")
        self.assertEqual((self.game_root / "profiles/Default/modlist.txt").read_text(), "# new\n")
        self.assertFalse((self.game_root / "mods").exists())
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})
        self.assertNotIn("test-mods", State.load().declined_folders)

    def test_old_versions_syncthing_kept_stay_with_the_copy(self):
        self.use_mods_spec()
        (self.kept / ".stversions/mods/Gone Mod").mkdir(parents=True)
        (self.kept / ".stversions/mods/Gone Mod/plugin~20261101.esp").write_text("old")
        self.service.share_game_folder("test-mods")
        self.assertEqual((self.kept / ".stversions/mods/Gone Mod/plugin~20261101.esp").read_text(), "old")

    def test_offers_of_it_are_left_alone_while_the_copy_moves_in(self):
        """Another process taking up the offer at the game's folder meanwhile
        would sync it there before the files have moved."""
        self.use_mods_spec()
        seen = []
        real = folders._use_kept

        def use_kept(spec, kept, root):
            seen.append(spec.key in State.load().declined_folders)
            return real(spec, kept, root)

        with patch.object(folders, "_use_kept", side_effect=use_kept):
            self.service.share_game_folder("test-mods")
        self.assertEqual(seen, [True])
        self.assertNotIn("test-mods", State.load().declined_folders)

    def test_a_release_syncthing_refuses_leaves_the_folder_offerable(self):
        self.use_mods_spec()
        self.st.refuse_delete = True
        with self.assertRaisesRegex(RuntimeError, "didn't let go"):
            self.service.share_game_folder("test-mods")
        self.assertNotIn("test-mods", State.load().declined_folders)

    def test_sharing_after_an_empty_kept_copy_starts_the_folder(self):
        self.spec = dataclasses.replace(self.spec, on_share=unittest.mock.Mock())
        self.service.hold_game_folder("test-mods")
        self.root = self.game_root
        self.service.share_game_folder("test-mods")
        self.spec.on_share.assert_called_once_with(self.game_root)

    def test_sharing_waits_while_syncthing_holds_on_to_the_kept_copy(self):
        self.service.hold_game_folder("test-mods")
        self.root = self.game_root
        self.st.refuse_delete = True
        with self.assertRaisesRegex(RuntimeError, "didn't let go"):
            self.service.share_game_folder("test-mods")
        self.assertEqual(State.load().held_folders, {"test-mods": str(self.kept)})

    def test_forgetting_mo2_or_leaving_the_vault_keeps_the_copies_and_machines(self):
        self.service.pair(PairingCode("DESKTOP", "", "Desktop"))
        self.service.hold_all_games(True)
        self.service.hold_game_folder("test-mods")
        State.update(instance_path=str(self.tmp / "MO2"), folder_id="modsync-1")
        self.service.reload_state()
        self.service.reset()
        st = State.load()
        self.assertEqual((st.held_folders, st.hold_all, st.paired), ({"test-mods": str(self.kept)}, True, True))
        self.assertIn("DESKTOP", self.st.devices)

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

    def test_forget(self):
        self.service.share_game_folder("test-mods")
        self.service.pair(PairingCode("OLDOLDO-RKWI4S2", "", ""))
        self.st.devices["OLDOLDO-RKWI4S2"]["name"] = "modorganizer2"
        code, out = self._run("forget", "laptop")
        self.assertEqual(code, 1)
        self.assertIn("No paired machine is called 'laptop'", out)
        code, out = self._run("forget", "oldoldo")
        self.assertEqual(code, 0, out)
        self.assertIn("Forgot OLDOLDO-RKWI4S2.", out)
        self.assertNotIn("OLDOLDO-RKWI4S2", self.st.devices)

    def test_join_with_a_machine_code_that_fails_says_so(self):
        text = PairingCode("DESKTOP", "", "ModSync").encode()
        with patch.object(self.service, "pair", side_effect=RuntimeError("Syncthing did not start")):
            code, out = self._run("join", text)
        self.assertEqual(code, 1)
        self.assertIn("Couldn't pair: Syncthing did not start", out)

    def test_hold(self):
        self.root = None
        code, out = self._run("hold", "test-mods")
        self.assertEqual(code, 0, out)
        self.assertIn("Keeping a copy of Test mods (ModSync)", out)
        self.assertIn("No machine is paired yet", out)
        code, out = self._run("status")
        self.assertIn("copy kept for other machines", out)
        code, out = self._run("hold", "all")
        self.assertEqual(code, 0, out)
        self.assertTrue(State.load().hold_all)
        code, out = self._run("hold", "off")
        self.assertEqual(code, 0, out)
        self.assertFalse(State.load().hold_all)
        code, out = self._run("hold", "nope")
        self.assertEqual(code, 2)
        self.assertIn("'all' / 'off'", out)

    def test_share_after_holding_says_where_the_old_copy_is(self):
        self.root, game = None, self.root
        self._run("hold", "test-mods")
        self.root = game
        code, out = self._run("share", "test-mods")
        self.assertEqual(code, 0, out)
        self.assertIn(f"{folders.held_root(self.spec)} isn't synced any more", out)

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

    def test_a_damaged_kept_copies_field_does_not_take_the_rest_with_it(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            State.path().parent.mkdir(parents=True)
            State.path().write_text('{"dfu_path": "/dfu", "held_folders": ["x"]}')
            st = State.load()
            self.assertEqual((st.dfu_path, st.held_folders, st.hold_all), ("/dfu", {}, False))

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


class BackgroundUpkeep(GameFolderBase):
    """The background service tidies the games' folders without reading their status."""

    def test_upkeep_tidies_folders_synced_here_only(self):
        tidied = []
        self.spec = folders.FolderSpec("test-mods", "Test mods (ModSync)", lambda: self.root,
                                       ("/*",), tidy=tidied.append)
        State.update(game_folders={"test-mods": str(self.root)}, held_folders={"test-mods-kept": "/kept"})
        self.service.reload_state()
        self.service.upkeep()
        self.assertEqual(tidied, [self.root])
        State.update(declined_folders=["test-mods"])  # stopping: left alone
        self.service.reload_state()
        self.service.upkeep()
        self.assertEqual(tidied, [self.root])

    def test_a_failing_tidy_never_stops_it(self):
        def broken(root):
            raise OSError("read-only")

        self.spec = folders.FolderSpec("test-mods", "Test mods (ModSync)", lambda: self.root, ("/*",), tidy=broken)
        State.update(game_folders={"test-mods": str(self.root)})
        self.service.reload_state()
        with self.assertLogs("modsync.sync.folders", "WARNING"):
            self.service.upkeep()


class SyncEvents(GameFolderBase):
    """Between passes the background service waits on Syncthing's events."""

    def test_it_follows_event_ids_and_starts_over_when_syncthing_does_not_answer(self):
        calls = []
        replies = [[{"id": 7, "type": "PendingDevicesChanged"}], [], OSError("connection refused"), []]

        def events(since, types, timeout):
            calls.append(since)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(_Client, "events", lambda self, *a: events(*a), create=True):
            self.assertTrue(self.service.wait_for_sync_events(30))
            self.assertTrue(self.service.wait_for_sync_events(30))
            self.assertFalse(self.service.wait_for_sync_events(30))
            self.assertTrue(self.service.wait_for_sync_events(30))
        self.assertEqual(calls, [0, 7, 7, 0])


if __name__ == "__main__":
    unittest.main()
