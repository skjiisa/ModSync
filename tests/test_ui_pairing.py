"""Network discovery placeholders must never select stale pairing targets."""

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None

from modsync.pairing_lan import Announcement
from modsync.state import State


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class PairingRescanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dashboard_never_joins_a_stale_peer_during_or_after_failed_scan(self):
        from unittest.mock import Mock
        from modsync.ui.sync_card import SyncCard

        service = Mock(state=State(instance_path="/unused/MO2"))
        with patch("modsync.ui.sync_card.run_async"):
            card = SyncCard(service)
        old = Announcement("Old peer", "192.0.2.1", 1234, "old-session")
        new = Announcement("New peer", "192.0.2.2", 1234, "new-session")
        card._on_scanned([old])
        with patch("modsync.ui.sync_card.run_async") as run, patch.object(card, "_ask_pin") as ask:
            card._scan()
            run.assert_called_once()  # discovery only
            run.reset_mock()
            card._scan()  # repeated requests cannot overlap
            card._on_machine_picked(card._net_list.item(0))  # the "Scanning…" row
            ask.assert_not_called()
            card._on_scan_failed("test timeout")
            card._on_machine_picked(card._net_list.item(0))  # the error row
            ask.assert_not_called()
            card._scan()
            run.reset_mock()
            card._on_scanned([new])
            card._on_machine_picked(card._net_list.item(0))
            ask.assert_called_once_with(new)
            run.assert_not_called()  # picking only asks; nothing joins by itself
        service.join_via_network.assert_not_called()

    def test_dashboard_pin_dialog_result_starts_the_join(self):
        from unittest.mock import Mock
        from modsync.ui.sync_card import SyncCard

        service = Mock(state=State(instance_path="/unused/MO2"))
        with patch("modsync.ui.sync_card.run_async"):
            card = SyncCard(service)
        peer = Announcement("Deck", "192.0.2.2", 21029, "s")
        dlg = Mock()
        dlg.exec.return_value = 1  # Accepted
        dlg.announcement.return_value = peer
        dlg.pin.return_value = "123456"
        with patch("modsync.ui.sync_card.PinDialog", return_value=dlg), \
                patch("modsync.ui.sync_card.run_async") as run:
            card._ask_pin(peer)
        self.assertEqual(run.call_args.args[:4], (service.join_via_network, peer, "123456", "/unused/MO2"))
        dlg.exec.return_value = 0  # Cancelled
        with patch("modsync.ui.sync_card.PinDialog", return_value=dlg), \
                patch("modsync.ui.sync_card.run_async") as run:
            card._ask_pin(peer)
        run.assert_not_called()

    def test_wizard_disables_finish_until_a_fresh_peer_is_selected(self):
        from unittest.mock import Mock
        from modsync.ui.wizard import WizardWidget

        with patch("modsync.ui.game_card.run_async"):
            wizard = WizardWidget(Mock(state=State()))
        wizard._go_to(2)  # sync comes before the game-version step
        page = wizard._vault
        old = Announcement("Old peer", "192.0.2.1", 1234, "old-session")
        new = Announcement("New peer", "192.0.2.2", 1234, "new-session")
        with patch("modsync.ui.wizard.run_async") as run:
            page.network_radio.setChecked(True)
            page._on_scanned([old])
            page._net_list.setCurrentRow(0)
            page._pin_edit.setText("123456")
            self.assertTrue(wizard._next.isEnabled())
            page._scan()
            self.assertFalse(wizard._next.isEnabled())
            run.reset_mock()
            page._scan()
            run.assert_not_called()
            page._net_list.setCurrentRow(0)
            self.assertIsNone(page.announcement)
            self.assertFalse(wizard._next.isEnabled())
            page._on_scan_failed("test timeout")
            page._net_list.setCurrentRow(0)
            self.assertIsNone(page.announcement)
            self.assertFalse(wizard._next.isEnabled())
            page._scan()
            page._on_scanned([new])
            self.assertFalse(wizard._next.isEnabled())
            page._net_list.setCurrentRow(0)
            self.assertIs(page.announcement, new)
            self.assertTrue(wizard._next.isEnabled())


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class FirewallHintTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _card(self, state: State):
        from unittest.mock import Mock
        from modsync.ui.sync_card import SyncCard

        with patch("modsync.ui.sync_card.run_async"):
            return SyncCard(Mock(state=state))

    def _rows(self, card):
        return [card._net_list.item(i).text() for i in range(card._net_list.count())]

    def test_empty_scan_points_at_the_firewall_row_when_blocked(self):
        from modsync.firewall import Check, Firewall

        card = self._card(State(instance_path="/unused/MO2"))
        card.firewall_checked(Check(Firewall("ufw"), False, "ufw:1"))
        card._on_scanned([])
        self.assertTrue(any("ufw is on here" in r and "On this machine" in r for r in self._rows(card)))

    def test_empty_scan_is_generic_when_nothing_blocks(self):
        from modsync import pairing_lan
        from modsync.firewall import Check, Firewall

        for chk in (Check(None, True, ""), Check(Firewall("ufw"), True, "ufw:1")):
            card = self._card(State(instance_path="/unused/MO2"))
            card.firewall_checked(chk)
            card._on_scanned([])
            self.assertIn(pairing_lan.FIREWALL_HINT, self._rows(card))


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class PinDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pair_is_enabled_only_with_six_digits_and_regroups_them(self):
        from modsync.ui.pin_dialog import PinDialog

        dlg = PinDialog(None, Announcement("Deck", "192.0.2.2", 21029, "s"))
        self.assertIn("Deck", dlg.windowTitle())
        self.assertFalse(dlg._pair_btn.isEnabled())
        dlg._pin_edit.setText("04281")
        self.assertFalse(dlg._pair_btn.isEnabled())
        dlg._pin_edit.setText("042815")
        self.assertEqual(dlg._pin_edit.text(), "042 815")
        self.assertEqual(dlg.pin(), "042815")
        self.assertTrue(dlg._pair_btn.isEnabled())
        dlg._pin_edit.setText("04 28 15 9")  # junk spacing and an extra digit
        self.assertEqual(dlg.pin(), "042815")

    def test_dialog_is_wide_enough_for_six_digits_at_the_big_font(self):
        from PySide6.QtGui import QFontMetrics
        from modsync.ui.pin_dialog import PinDialog

        dlg = PinDialog(None, Announcement("steamdeck", "192.0.2.2", 21029, "s"))
        dlg.show()
        self.app.processEvents()
        needed = QFontMetrics(dlg._pin_edit.font()).horizontalAdvance("888 888")
        self.assertGreater(dlg._pin_edit.width(), needed + 40)  # digits plus padding, never clipped
        self.assertGreaterEqual(dlg.width(), 420)

    def test_address_mode_needs_an_address_and_builds_a_manual_announcement(self):
        from modsync.pairing_lan import PairError
        from modsync.ui.pin_dialog import PinDialog

        dlg = PinDialog(None)
        dlg._pin_edit.setText("123456")
        self.assertFalse(dlg._pair_btn.isEnabled())
        dlg._addr_edit.setText("192.168.1.20:5000")
        self.assertTrue(dlg._pair_btn.isEnabled())
        ann = dlg.announcement()
        self.assertEqual((ann.host, ann.port), ("192.168.1.20", 5000))
        dlg._addr_edit.setText("host:notaport")
        with self.assertRaises(PairError):
            dlg.announcement()

    def test_host_card_shows_a_big_pin_while_pairing_and_restores_after(self):
        from unittest.mock import Mock
        from modsync.ui.sync_card import SyncCard

        state = State(instance_path="/unused/MO2", folder_id="modsync-x")
        with patch("modsync.ui.sync_card.run_async"):
            card = SyncCard(Mock(state=state))
        self.assertFalse(card._pin_panel.isVisibleTo(card))
        with patch("modsync.ui.sync_card.run_async") as run, \
                patch("modsync.ui.sync_card.pairing_lan.make_pin", return_value="042815"):
            card._pair_network()
        self.assertTrue(card._pin_panel.isVisibleTo(card))
        self.assertFalse(card._share_normal.isVisibleTo(card))
        self.assertEqual(card._pin_label.text(), "042 815")
        self.assertIn("Copy from another machine", card._pin_steps.text())
        run.call_args.kwargs["on_ready"](Announcement("me", "192.0.2.9", 21029, "s"))
        self.app.processEvents()
        self.assertIn("192.0.2.9", card._pin_addr.text())
        card._pair_network()  # cancel
        self.assertFalse(card._pin_panel.isVisibleTo(card))
        self.assertTrue(card._share_normal.isVisibleTo(card))


@unittest.skipIf(QApplication is None, "PySide6 not installed")
class SyncThenVersionTests(unittest.TestCase):
    """After joining, the vault record and SKSE arrive by sync; the dashboard
    must notice on its own instead of waiting for a manual refresh."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_game_card_refreshes_when_the_vault_record_changes(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import Mock
        from modsync import gameversion
        from modsync.ui.game_card import GameCard

        with tempfile.TemporaryDirectory() as tmp:
            state = State(instance_path=tmp)
            with patch("modsync.ui.game_card.run_async"):
                card = GameCard(Mock(state=state))
            with patch.object(card, "refresh") as refresh:
                card.poll()  # nothing changed
                refresh.assert_not_called()
                gameversion.record_vault_version(tmp, gameversion.GameVersion.parse("1.5.97"))
                card.poll()
                refresh.assert_called_once()

    def test_sync_card_announces_the_first_full_sync_only_once(self):
        from unittest.mock import Mock
        from modsync.service import SyncStatus
        from modsync.ui.sync_card import SyncCard

        state = State(instance_path="/unused/MO2", folder_id="modsync-x")
        with patch("modsync.ui.sync_card.run_async"):
            card = SyncCard(Mock(state=state))
        synced = []
        card.synced.connect(lambda: synced.append(True))

        def status(state_, pct):
            return SyncStatus("ME", "modsync-x", True, state_, pct, [])

        card._on_status(status("syncing", 40.0))
        card._on_status(status("syncing", 99.6))
        self.assertEqual(synced, [])
        card._on_status(status("idle", 100.0))
        self.assertEqual(synced, [True])
        card._on_status(status("idle", 100.0))  # steady state: no repeat
        self.assertEqual(synced, [True])
        card._on_status(status("syncing", 80.0))  # more mods arriving...
        card._on_status(status("idle", 100.0))  # ...and done again
        self.assertEqual(synced, [True, True])
