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
            wizard = WizardWidget(Mock())
        wizard._go_to(3)
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
class FirewallBannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _card(self, state: State):
        from unittest.mock import Mock
        from modsync.ui.sync_card import SyncCard

        with patch("modsync.ui.sync_card.run_async"):  # no real detection
            return SyncCard(Mock(state=state))

    def test_banner_appears_for_a_blocking_firewall_and_tailors_the_empty_scan(self):
        from modsync.firewall import Check, Firewall

        card = self._card(State(instance_path="/unused/MO2"))
        self.assertFalse(card._fw_banner.isVisibleTo(card))
        card._on_firewall_checked(Check(Firewall("ufw"), False, "ufw:1"))
        self.assertTrue(card._fw_banner.isVisibleTo(card))
        self.assertIn("ufw is on", card._fw_label.text())
        card._on_scanned([])
        rows = [card._net_list.item(i).text() for i in range(card._net_list.count())]
        self.assertTrue(any("ufw is on here" in r for r in rows), rows)

    def test_no_firewall_means_no_banner_and_generic_hint(self):
        from modsync import pairing_lan
        from modsync.firewall import Check

        card = self._card(State(instance_path="/unused/MO2"))
        card._on_firewall_checked(Check(None, True, ""))
        self.assertFalse(card._fw_banner.isVisibleTo(card))
        card._on_scanned([])
        rows = [card._net_list.item(i).text() for i in range(card._net_list.count())]
        self.assertIn(pairing_lan.FIREWALL_HINT, rows)

    def test_allowing_hides_the_banner_and_remembers_the_rules_stamp(self):
        import tempfile
        from pathlib import Path
        from modsync.firewall import Check, Firewall

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(State, "path", return_value=Path(tmp) / "state.json"):
                state = State(instance_path="/unused/MO2")
                card = self._card(state)
                card._on_firewall_checked(Check(Firewall("ufw"), False, "ufw:1"))
                card._on_firewall_allowed("ufw:2")
                self.assertFalse(card._fw_banner.isVisibleTo(card))
                self.assertEqual(State.load().firewall_rules_stamp, "ufw:2")
                # Next launch: the rules were read and found open -> no banner...
                card2 = self._card(State.load())
                card2._on_firewall_checked(Check(Firewall("ufw"), True, "ufw:2"))
                self.assertFalse(card2._fw_banner.isVisibleTo(card2))
                # ...but rules found closed again (ufw delete) bring it back.
                card2._on_firewall_checked(Check(Firewall("ufw"), False, "ufw:3"))
                self.assertTrue(card2._fw_banner.isVisibleTo(card2))


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
