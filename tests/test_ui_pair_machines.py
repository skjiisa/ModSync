"""The Sync page without a vault (#117): a machine that syncs only games'
folders pairs from the app, shows its own code and PIN, and lists the paired
machines and each game's folder."""

from unittest.mock import patch

from modsync import dfu
from modsync.pairing_code import PairingCode
from modsync.pairing_lan import Announcement, PairPayload
from modsync.service import DeviceStatus, ModSyncService, SyncStatus
from modsync.state import State
from modsync.sync.folders import FolderStatus
from tests.ui_support import UiTestCase

DECK = Announcement("steamdeck", "192.0.2.2", 21029, "s")


def _status(game_folders=(), devices=()):
    return SyncStatus("ME", None, False, None, None, [DeviceStatus(*d) for d in devices],
                      game_folders=list(game_folders))


class NothingSyncedTests(UiTestCase):
    def test_a_machine_without_mo2_can_pair_but_not_host(self):
        with patch.object(ModSyncService, "status") as status:
            window = self.window()
            sync = window.pages["sync"]
            self.settle()
        status.assert_not_called()  # nothing to ask Syncthing about: it isn't started for this
        self.assertFalse(sync.live)
        self.assertFalse(sync.pair_tile.isVisibleTo(sync))  # no code or PIN until something syncs here
        self.assertFalse(sync.code_tile.isVisibleTo(sync))
        self.assertFalse(sync.ui_tile.isVisibleTo(sync))
        self.assertTrue(sync.join_tile.isVisibleTo(sync))
        self.assertEqual(sync.join_tile.property("tileRole"), "primary")
        self.assertTrue(sync._no_games.isVisibleTo(sync))
        self.assertIs(sync.preferred_focus(), sync.join_tile)
        sync.choose_first.click()  # MO2's sync is still a step away
        self.assertIsNotNone(window.top_overlay)

    def test_turning_a_games_sync_on_elsewhere_offers_the_pin(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        sync = window.pages["sync"]
        self.assertTrue(sync.share.isVisibleTo(sync))  # MO2's choice comes first
        self.assertTrue(sync.pair_only_tile.isVisibleTo(sync))  # and pairing for games is one row under it
        self.assertFalse(sync.machine_box.isVisibleTo(sync))
        sync.pair_only_tile.click()
        self.assertTrue(sync.pair_join_panel.isVisibleTo(sync))
        State.update(game_folders={"dfu-mods": str(self.tmp / "dfu")})
        with patch.object(ModSyncService, "status", lambda self: _status()), \
                patch.object(ModSyncService, "my_pairing_code", lambda self: PairingCode("ME" * 28, "", "ModSync")):
            sync.poll()
            self.settle()
        self.assertFalse(sync.pair_only_tile.isVisibleTo(sync))
        self.assertTrue(sync.machine_box.isVisibleTo(sync))
        self.assertTrue(sync.pair_tile.isVisibleTo(sync))
        # The choice above stays the one in blue.
        self.assertEqual(sync.pair_tile.property("tileRole"), "normal")
        self.assertEqual(sync.join_tile.property("tileRole"), "normal")
        self.assertIn("games' folders only", sync.join_tile.description)
        self.assertTrue(sync._code.startswith("MODSYNC2-"))


class GamesOnlyTests(UiTestCase):
    def setUp(self):
        super().setUp()
        self.mods = str(self.tmp / "dfu")
        State(game_folders={"dfu-mods": self.mods}, paired=True).save()
        code = PairingCode("ME" * 28, "", "ModSync")
        for p in (patch.object(ModSyncService, "status", lambda svc: self.status),
                  patch.object(ModSyncService, "my_pairing_code", lambda svc: code)):
            p.start()
            self.addCleanup(p.stop)
        self.status = _status(
            [FolderStatus(dfu.MODS_FOLDER, self.mods, "idle", 100.0)], [("DECKDEC-1", "steamdeck", True)])

    def test_lists_the_games_folders_and_machines_and_offers_the_pin(self):
        window = self.window()
        sync = window.pages["sync"]
        self.settle()
        self.assertTrue(sync.pair_tile.isVisibleTo(sync))
        self.assertEqual(sync.pair_tile.property("tileRole"), "primary")
        self.assertEqual(sync.join_tile.property("tileRole"), "normal")
        self.assertTrue(sync.ui_tile.isVisibleTo(sync))
        row = sync._game_rows["dfu-mods"]
        self.assertEqual((row.text(), row.badge.text()), ("Daggerfall Unity mods", "Up to date"))
        self.assertFalse(sync._no_games.isVisibleTo(sync))
        self.assertEqual([r.text() for r in sync._device_rows.values()], ["steamdeck"])

        self.status = _status([FolderStatus(dfu.MODS_FOLDER, self.mods, "syncing", 40.0, need_items=3)])
        sync.poll()
        self.settle()
        self.assertIs(sync._game_rows["dfu-mods"], row)  # updated in place
        self.assertEqual(row.badge.text(), "Syncing 40%")
        self.assertEqual(row.description, "3 items to go.")
        self.assertEqual(sync._device_rows, {})

        with patch.object(window, "switch_game") as switch:
            row.click()
        switch.assert_called_once()
        self.assertEqual(switch.call_args.args[0], "daggerfall")

    def test_a_folder_conflict_or_stop_shows_on_its_row(self):
        window = self.window()
        sync = window.pages["sync"]
        sync._on_machine_status(_status([FolderStatus(dfu.MODS_FOLDER, self.mods, "idle", 100.0, conflicts=2)]))
        self.assertEqual(sync._game_rows["dfu-mods"].badge.text(), "2 conflicts")
        sync._on_machine_status(_status([FolderStatus(dfu.MODS_FOLDER, self.mods, stopping=True)]))
        self.assertEqual(sync._game_rows["dfu-mods"].badge.text(), "Stopping")
        sync._on_machine_status(_status())
        self.assertEqual(sync._game_rows, {})
        self.assertTrue(sync._no_games.isVisibleTo(sync))

    def test_the_pin_and_code_tell_the_other_machine_to_pair(self):
        window = self.window()
        sync = window.pages["sync"]
        self.settle()
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            sync.pair_tile.click()
        self.assertEqual(run.call_args.args[0], window.service.host_network_pairing)
        self.assertIn("Pair with another machine", window.top_overlay.steps.text())
        sync.cancel_pairing()
        sync.code_tile.click()
        self.assertIn("Pair with another machine", window.top_overlay.steps.text())

    def test_a_pin_entered_here_only_pairs(self):
        window = self.window()
        sync = window.pages["sync"]
        sync.join_tile.click()
        self.assertTrue(sync.pair_join_panel.isVisibleTo(sync))
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            sheet = sync.pair_join.ask_pin(DECK)
            sheet.set_pin("123456")
            sheet.submit()
        self.assertEqual(run.call_args.args, (window.service.join_via_network, DECK, "123456", None))
        with patch.object(window, "rebuild") as rebuild:
            run.call_args.kwargs["on_done"](PairPayload("DECK", name="steamdeck"))
        rebuild.assert_called_once()
        self.assertIn("Paired with steamdeck. The games you sync here now sync with it too.", window.messages)
        self.assertFalse(window.busy)

    def test_a_pasted_code_only_pairs_even_a_vaults(self):
        window = self.window()
        sync = window.pages["sync"]
        vault = PairingCode("D" * 56, "modsync-vault", "Skyrim SE")
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            sync.pair_join.join_code(vault.encode())
        self.assertEqual(run.call_args.args, (window.service.pair, vault))


class VaultJoinTests(UiTestCase):
    """"Copy from another machine" pairs with a machine that has no vault
    instead of failing."""

    def _join(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        return window, window.pages["sync"].join

    def test_a_machine_code_pairs(self):
        window, panel = self._join()
        code = PairingCode("D" * 56, "", "ModSync")
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            panel.join_code(code.encode())
        self.assertEqual(run.call_args.args, (window.service.pair, code))
        self.assertIsNone(window.top_overlay)  # copy or merge isn't asked: nothing is copied
        with patch.object(window, "rebuild"):
            run.call_args.kwargs["on_done"](None)
        self.assertTrue(any("doesn't share Mod Organizer 2" in m for m in window.messages))

    def test_a_host_without_a_vault_pairs(self):
        window, panel = self._join()
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            panel.join_network(DECK, "123456")
        self.assertTrue(run.call_args.kwargs["pair_only"])
        with patch.object(window, "vault_joined") as joined, patch.object(window, "rebuild"):
            run.call_args.kwargs["on_done"](PairPayload("DECK", name="steamdeck"))
            joined.assert_not_called()
            self.assertIn("Paired with steamdeck. It doesn't share Mod Organizer 2, so only games' folders sync "
                          "with it.", window.messages)
            run.call_args.kwargs["on_done"](PairPayload("DECK", "modsync-vault", "Skyrim SE"))
            joined.assert_called_once()


class VaultPageTests(UiTestCase):
    def test_the_games_folders_show_beside_the_vault_when_there_are_any(self):
        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        window = self.window()
        sync = window.pages["sync"]
        self.settle()
        self.assertFalse(sync.games_panel.isVisibleTo(sync))
        status = SyncStatus("ME", "modsync-1", True, "idle", 100.0, [],
                            game_folders=[FolderStatus(dfu.SAVES_FOLDER, str(self.tmp / "saves"), "idle", 100.0)])
        sync.on_status(status)
        self.assertTrue(sync.games_panel.isVisibleTo(sync))
        self.assertEqual(sync._game_rows["dfu-saves"].text(), "Daggerfall Unity saves")
