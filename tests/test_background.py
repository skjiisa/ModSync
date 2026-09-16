import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modsync import background


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.unit = Path(tmp.name) / background.UNIT_NAME
        self.patch_path = patch.object(background, "unit_path", return_value=self.unit)
        self.patch_path.start()
        self.addCleanup(self.patch_path.stop)

    @staticmethod
    def result(code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_start_failure_is_reported(self):
        with patch.object(background, "_systemctl", side_effect=[
            self.result(), self.result(1, stderr="Access denied")
        ]):
            with self.assertRaisesRegex(RuntimeError, "Access denied"):
                background.install()

    def test_inactive_service_is_not_reported_as_started(self):
        with patch.object(background, "_systemctl", side_effect=[
            self.result(), self.result(), self.result(3, stdout="failed")
        ]):
            with self.assertRaisesRegex(RuntimeError, "is-active.*failed"):
                background.install()

    def test_success_checks_service_is_active(self):
        with patch.object(background, "_systemctl", return_value=self.result()) as call:
            self.assertIn("Enabled and started", background.install())
        self.assertIn("modsync serve", self.unit.read_text())
        call.assert_any_call("is-active", background.UNIT_NAME)

    def test_failed_stop_preserves_service_file(self):
        self.unit.write_text("service")
        with patch.object(background, "_systemctl", return_value=self.result(1, stderr="denied")):
            with self.assertRaisesRegex(RuntimeError, "denied"):
                background.uninstall()
        self.assertTrue(self.unit.exists())

    def test_status_distinguishes_installed_from_running(self):
        self.unit.write_text("service")
        with patch.object(background, "_systemctl", side_effect=[
            self.result(3, stdout="failed\n"), self.result(stdout="enabled\n")
        ]):
            status = background.status()
        self.assertTrue(status["installed"])
        self.assertEqual(status["active"], "failed")
        self.assertEqual(status["enabled"], "enabled")
