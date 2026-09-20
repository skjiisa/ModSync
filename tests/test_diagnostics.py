"""File logging and the diagnostics bundle: writes where XDG says, never fails
when it cannot, and leaks no pairing code / API key / device id."""

import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import __version__, diagnostics, logging_setup
from modsync.__main__ import main
from modsync.pairing_code import PairingCode

FAKE_API_KEY = "kPq7ZmX2vR9tL4wN8cB1dF6hJ3sA5gYe"
FAKE_DEVICE_ID = "ABCDEFG-HIJKLMN-OPQRSTU-VWXYZ23-4567ABC-DEFGHIJ-KLMNOPQ-RSTUVWX"


class _StateHome(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_home = self.tmp / "state"
        self.previous_state_home = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.state_home)
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "data")
        self.addCleanup(os.environ.pop, "XDG_DATA_HOME", None)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.previous_state_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = self.previous_state_home
        # Drop our handlers so the next test starts clean.
        for h in list(logging.getLogger().handlers):
            if getattr(h, logging_setup._MARKER, False):
                logging.getLogger().removeHandler(h)
                h.close()


class ConfigureTests(_StateHome):
    def test_writes_under_xdg_state_home(self):
        path = logging_setup.configure("cli", ["sync", "join", PairingCode(FAKE_DEVICE_ID, "modsync-1").encode(), "/x"])
        self.assertEqual(path, self.state_home / "modsync" / "modsync.log")
        logging.getLogger("modsync.test").info("hello from the test")
        text = path.read_text(encoding="utf-8")
        self.assertIn(f"ModSync {__version__} starting (cli, argv=", text)
        self.assertIn("[cli] modsync.test: hello from the test", text)
        self.assertIn("MODSYNC1-[redacted]", text)
        self.assertNotIn(PairingCode(FAKE_DEVICE_ID, "modsync-1").encode(), text)

    def test_unwritable_dir_falls_back_to_stderr(self):
        blocker = self.state_home
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("not a directory")  # mkdir("state/modsync") must fail
        err = io.StringIO()
        with patch.object(sys, "stderr", err):
            path = logging_setup.configure("gui", [])
            logging.getLogger("modsync.test").warning("still visible")
        self.assertIsNone(path)
        self.assertIn("still visible", err.getvalue())
        self.assertIn("logging to stderr only", err.getvalue())

    def test_log_level_env(self):
        with patch.dict(os.environ, {"MODSYNC_LOG_LEVEL": "debug"}):
            path = logging_setup.configure("serve", [])
        logging.getLogger("modsync.test").debug("fine detail")
        self.assertIn("fine detail", path.read_text(encoding="utf-8"))

    def test_launch_hook_log_lives_next_to_ours(self):
        self.assertEqual(logging_setup.launch_hook_log_path(), self.state_home / "modsync" / "launch-hook.log")


class RedactTests(unittest.TestCase):
    def test_shapes(self):
        code = PairingCode(FAKE_DEVICE_ID, "modsync-abc", "Deck").encode()
        text = (
            f"join {code} now\n"
            f"X-API-Key: {FAKE_API_KEY}\n"
            f"<apikey>{FAKE_API_KEY}</apikey>\n"
            f"device {FAKE_DEVICE_ID} connected\n"
            "build 1234567 unchanged"
        )
        out = diagnostics.redact(text, secrets=[])
        self.assertNotIn(code, out)
        self.assertNotIn(FAKE_API_KEY, out)
        self.assertNotIn(FAKE_DEVICE_ID, out)
        self.assertIn("MODSYNC1-[redacted]", out)
        self.assertIn("X-API-Key: [redacted]", out)
        self.assertIn("<apikey>[redacted]</apikey>", out)
        self.assertIn("device ABCDEFG-… connected", out)
        self.assertIn("build 1234567 unchanged", out)

    def test_known_secret_anywhere(self):
        out = diagnostics.redact(f"raw {FAKE_API_KEY} in a line", secrets=[FAKE_API_KEY])
        self.assertEqual(out, "raw [redacted] in a line")


class DiagnosticsCommandTests(_StateHome):
    def test_output_has_version_report_and_redacted_logs(self):
        log_dir = self.state_home / "modsync"
        log_dir.mkdir(parents=True)
        code = PairingCode(FAKE_DEVICE_ID, "modsync-abc").encode()
        (log_dir / "modsync.log").write_text(
            "\n".join(f"line {i}" for i in range(300)) + f"\njoined with {code}\n", encoding="utf-8"
        )
        (log_dir / "launch-hook.log").write_text(f"hub apikey={FAKE_API_KEY}\n", encoding="utf-8")
        # A Syncthing config with a real-looking key: it must be redacted wherever it appears.
        st_home = self.tmp / "data" / "modsync" / "syncthing"
        st_home.mkdir(parents=True)
        (st_home / "config.xml").write_text(
            f"<configuration><gui><apikey>{FAKE_API_KEY}</apikey><address>127.0.0.1:1</address></gui></configuration>"
        )

        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["diagnostics"])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(f"ModSync {__version__}", text)
        self.assertIn(f"Python {sys.version_info.major}.{sys.version_info.minor}", text)
        self.assertIn("Flatpak: ", text)
        self.assertIn("=== doctor ===", text)
        self.assertIn("Platform:", text)
        self.assertIn("=== modsync.log", text)
        self.assertIn("=== launch-hook.log", text)
        self.assertNotIn(code, text)
        self.assertNotIn(FAKE_API_KEY, text)
        self.assertIn("MODSYNC1-[redacted]", text)
        self.assertNotIn("line 99\n", text)  # only the last 200 lines
        self.assertIn("line 299", text)

    def test_missing_logs_are_reported_not_fatal(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["diagnostics"])
        self.assertEqual(rc, 0)
        self.assertIn("(no such file)", out.getvalue())

    def test_doctor_prints_log_path(self):
        out = io.StringIO()
        with redirect_stdout(out):
            main(["doctor"])
        self.assertIn(f"Log file:        {self.state_home / 'modsync' / 'modsync.log'}", out.getvalue())

    def test_help_lists_diagnostics(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("modsync diagnostics", out.getvalue())


class StdlibOnlyTests(unittest.TestCase):
    """`doctor` and `diagnostics` must run with only the standard library:
    the third-party imports are blocked in a fresh interpreter."""

    _BLOCK = (
        "import sys\n"
        "for name in ('PySide6', 'httpx', 'platformdirs', 'qrcode', 'spake2', 'cryptography'):\n"
        "    sys.modules[name] = None\n"
        "from modsync.__main__ import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n"
    )

    def _run(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, XDG_STATE_HOME=tmp, XDG_CONFIG_HOME=tmp, XDG_DATA_HOME=tmp)
            return subprocess.run(
                [sys.executable, "-c", self._BLOCK, *args],
                capture_output=True, text=True, env=env, cwd=str(Path(__file__).resolve().parents[1]),
            )

    def test_doctor(self):
        result = self._run("doctor")
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("Platform:", result.stdout)
        self.assertIn("Log file:", result.stdout)

    def test_diagnostics(self):
        result = self._run("diagnostics")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"ModSync {__version__}", result.stdout)
        self.assertIn("=== doctor ===", result.stdout)
