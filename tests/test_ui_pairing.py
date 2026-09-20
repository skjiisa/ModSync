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
        card = SyncCard(service)
        messages = []
        card.status.connect(messages.append)
        old = Announcement("Old peer", "192.0.2.1", 1234, "old-session")
        new = Announcement("New peer", "192.0.2.2", 1234, "new-session")
        card._on_scanned([old])
        card._net_list.setCurrentRow(0)
        card._pin_edit.setText("123456")
        with patch("modsync.ui.sync_card.run_async") as run:
            card._scan()
            run.assert_called_once()  # discovery only
            run.reset_mock()
            card._scan()  # repeated requests cannot overlap
            card._net_list.setCurrentRow(0)  # the "Scanning…" row
            card._join_network()
            run.assert_not_called()
            self.assertIn("Pick a machine", messages[-1])
            card._on_scan_failed("test timeout")
            card._net_list.setCurrentRow(0)  # the error row
            card._join_network()
            run.assert_not_called()
            self.assertIn("Pick a machine", messages[-1])
            card._scan()
            card._on_scanned([new])
            card._net_list.setCurrentRow(0)
            run.reset_mock()
            card._join_network()
            self.assertIs(run.call_args.args[1], new)
        service.join_via_network.assert_not_called()

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
