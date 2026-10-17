"""Pairing in the UI: scan results, the PIN wheels, the host's big PIN, and
noticing when synced mods have arrived."""

import tempfile
from unittest.mock import patch

from modsync import gameversion, pairing_lan
from modsync.pairing_lan import Announcement
from modsync.service import ModSyncService, SyncStatus
from modsync.state import State
from tests.ui_support import UiTestCase


class JoinPanelTests(UiTestCase):
    def _panel(self):
        State(instance_path=str(self.tmp)).save()
        window = self.window()
        sync = window.pages["sync"]
        window.go("sync")
        return window, sync.join

    def test_placeholders_are_never_choosable_and_scans_never_overlap(self):
        window, panel = self._panel()
        old = Announcement("Old peer", "192.0.2.1", 1234, "old-session")
        new = Announcement("New peer", "192.0.2.2", 1234, "new-session")
        panel.on_scanned([old])
        self.assertEqual([t.text() for t in panel.machine_tiles], ["Old peer"])
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            panel.scan()
            run.assert_called_once()  # discovery only
            run.reset_mock()
            panel.scan()  # repeated requests cannot overlap
            run.assert_not_called()
            self.assertEqual(panel.machine_tiles, [])  # "Listening…" is text, not a choice
            panel.on_scan_failed("test timeout")
            self.assertEqual(panel.machine_tiles, [])
            self.assertIn("Scan failed: test timeout", panel.placeholder_texts)
            panel.scan()
            run.reset_mock()
            panel.on_scanned([new])
            panel.machine_tiles[0].click()
            self.assertEqual(window.top_overlay.title.text(), "Pair with New peer")
            run.assert_not_called()  # picking only asks; nothing joins by itself

    def test_pin_sheet_result_starts_the_join(self):
        window, panel = self._panel()
        peer = Announcement("Deck", "192.0.2.2", 21029, "s")
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            sheet = panel.ask_pin(peer)
            sheet.set_pin("123456")
            sheet.submit()
        self.assertEqual(run.call_args.args[:4], (window.service.join_via_network, peer, "123456", str(self.tmp)))
        self.assertIsNone(window.top_overlay)
        with patch("modsync.ui.pages.sync.worker.run_async") as run:
            sheet = panel.ask_pin(peer)
            sheet.set_pin("123456")
            sheet.cancel()
        run.assert_not_called()

    def test_empty_scan_points_at_the_firewall_tile_when_blocked(self):
        from modsync.firewall import Check, Firewall

        window, panel = self._panel()
        window.firewall_check = Check(Firewall("ufw"), False, "ufw:1")
        panel.on_scanned([])
        self.assertTrue(any("ufw is on here" in r and "Settings" in r for r in panel.placeholder_texts))

    def test_empty_scan_is_generic_when_nothing_blocks(self):
        from modsync.firewall import Check, Firewall

        window, panel = self._panel()
        for chk in (Check(None, True, ""), Check(Firewall("ufw"), True, "ufw:1")):
            window.firewall_check = chk
            panel.on_scanned([])
            self.assertIn(pairing_lan.FIREWALL_HINT, panel.placeholder_texts)

    def test_pasted_codes_are_checked_before_joining(self):
        from modsync.pairing_code import PairingCode

        window, panel = self._panel()
        panel.code_tile.click()
        sheet = window.top_overlay
        sheet.type_text("not a code")
        with patch.object(ModSyncService, "join_vault") as join:
            sheet.done()
            self.assertTrue(sheet.error.isVisibleTo(sheet))
            self.assertIs(window.top_overlay, sheet)  # stays open with the problem shown
            sheet.field.setText(PairingCode("A" * 56, "modsync-abc", "Deck").encode())
            sheet.done()
            self.settle()
        join.assert_called_once()


DECK = Announcement("Deck", "192.0.2.2", 21029, "s")


class PinSheetTests(UiTestCase):
    def _sheet(self, announcement=DECK):
        from modsync.ui.overlays import PinSheet

        window = self.window()
        done = []
        sheet = PinSheet(window, announcement, lambda ann, pin: done.append((ann, pin)))
        sheet.open()
        return window, sheet, done

    def test_pair_is_enabled_only_with_six_digits_and_regroups_them(self):
        window, sheet, done = self._sheet()
        self.assertIn("Deck", sheet.title.text())
        self.assertFalse(sheet.pair_button.isEnabled())
        sheet.set_pin("04281")
        self.assertFalse(sheet.pair_button.isEnabled())
        self.assertEqual(sheet.hint.text(), "5 of 6 digits")
        sheet.set_pin("042815")
        self.assertEqual(sheet.shown_pin, "042 815")
        self.assertEqual(sheet.pin(), "042815")
        self.assertTrue(sheet.pair_button.isEnabled())
        sheet.set_pin("04 28 15 9")  # junk spacing and an extra digit
        self.assertEqual(sheet.pin(), "042815")

    def test_wheels_turn_with_the_dpad_and_take_typed_digits(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        window, sheet, done = self._sheet()
        first = sheet.cells[0]
        self.assertIs(self.app.focusWidget(), first)
        QTest.keyClick(first, Qt.Key.Key_Up)
        QTest.keyClick(first, Qt.Key.Key_Up)
        self.assertEqual(first.value, "1")  # empty → 0 → 1
        QTest.keyClick(first, Qt.Key.Key_Down)
        QTest.keyClick(first, Qt.Key.Key_Down)
        self.assertEqual(first.value, "9")
        QTest.keyClick(first, Qt.Key.Key_Right)
        self.assertIs(self.app.focusWidget(), sheet.cells[1])
        for digit in "42815":  # each digit moves on to the next wheel
            QTest.keyClicks(self.app.focusWidget(), digit)
        self.assertEqual(sheet.pin(), "942815")
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Backspace)
        self.assertEqual(sheet.pin(), "94281")
        QTest.keyClicks(self.app.focusWidget(), "5")
        QTest.keyClick(self.app.focusWidget(), Qt.Key.Key_Return)  # A on a full PIN pairs
        self.assertEqual(done, [(sheet._announcement, "942815")])

    def test_address_mode_needs_an_address_and_builds_a_manual_announcement(self):
        window, sheet, done = self._sheet(None)
        sheet.set_pin("123456")
        self.assertFalse(sheet.pair_button.isEnabled())
        sheet.address_tile.click()
        keyboard = window.top_overlay
        keyboard.field.setText("host:notaport")
        keyboard.done()
        self.assertIn("Enter an address", keyboard.error.text())
        keyboard.field.setText("192.168.1.20:5000")
        keyboard.done()
        self.assertIs(window.top_overlay, sheet)
        self.assertTrue(sheet.pair_button.isEnabled())
        ann = sheet.announcement()
        self.assertEqual((ann.host, ann.port), ("192.168.1.20", 5000))
        sheet.submit()
        self.assertEqual(done[0][1], "123456")


class BeaconTests(UiTestCase):
    def test_host_shows_a_big_pin_while_pairing_and_cancels_cleanly(self):
        State(instance_path=str(self.tmp), folder_id="modsync-x").save()
        window = self.window()
        sync = window.pages["sync"]
        with patch("modsync.ui.pages.sync.worker.run_async") as run, \
                patch("modsync.ui.pages.sync.pairing_lan.make_pin", return_value="042815"):
            sync.pair_tile.click()
        beacon = window.top_overlay
        self.assertIs(beacon, sync.beacon)
        self.assertEqual(beacon.pin_label.text(), "042 815")
        self.assertIn("Copy from another machine", beacon.steps.text())
        run.call_args.kwargs["on_ready"](Announcement("me", "192.0.2.9", 21029, "s"))
        self.settle()
        self.assertIn("192.0.2.9", beacon.address.text())
        stop = run.call_args.kwargs["stop"]
        beacon.cancel()  # B
        self.assertTrue(stop.is_set())
        self.assertIsNone(window.top_overlay)
        self.assertIn("Network pairing cancelled.", window.messages)
        sync._on_pair_failed("late failure after cancel")  # ignored
        self.assertNotIn("⚠ Pairing: late failure after cancel", window.messages)

    def test_a_successful_pairing_closes_the_pin(self):
        State(instance_path=str(self.tmp), folder_id="modsync-x").save()
        window = self.window()
        sync = window.pages["sync"]
        with patch("modsync.ui.pages.sync.worker.run_async"):
            sync.pair_network()
            sync._on_paired(object())
        self.assertIsNone(window.top_overlay)
        self.assertIn("Paired with a new machine over the network!", window.messages)


class SyncThenVersionTests(UiTestCase):
    """After joining, the vault record and SKSE arrive by sync; the window
    must notice on its own instead of waiting for a manual refresh."""

    def test_game_panel_refreshes_when_the_vault_record_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            State(instance_path=tmp).save()
            window = self.window()
            panel = window.pages["game"].panel
            with patch.object(panel, "refresh") as refresh:
                panel.poll()  # nothing changed
                refresh.assert_not_called()
                gameversion.record_vault_version(tmp, gameversion.GameVersion.parse("1.5.97"))
                panel.poll()
                refresh.assert_called_once()

    def test_sync_announces_the_first_full_sync_only_once(self):
        State(instance_path=str(self.tmp), folder_id="modsync-x").save()
        window = self.window()
        sync = window.pages["sync"]
        sync._was_complete = None
        synced = []
        sync.synced.connect(lambda: synced.append(True))

        def status(state_, pct):
            return SyncStatus("ME", "modsync-x", True, state_, pct, [])

        sync.on_status(status("syncing", 40.0))
        sync.on_status(status("syncing", 99.6))
        self.assertEqual(synced, [])
        sync.on_status(status("idle", 100.0))
        self.assertEqual(synced, [True])
        sync.on_status(status("idle", 100.0))  # steady state: no repeat
        self.assertEqual(synced, [True])
        sync.on_status(status("syncing", 80.0))  # more mods arriving...
        sync.on_status(status("idle", 100.0))  # ...and done again
        self.assertEqual(synced, [True, True])
