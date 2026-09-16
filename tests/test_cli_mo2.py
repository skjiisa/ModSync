"""`modsync mo2` manages the instance without any sync involved."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import gameversion
from modsync.__main__ import main
from modsync.state import State


class Mo2CommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        no_game = patch.object(gameversion, "find_game_dir", return_value=None)
        no_game.start()
        self.addCleanup(no_game.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_status_before_anything(self):
        code, out = self.run_cli("mo2", "status")
        self.assertEqual(code, 1)
        self.assertIn("No Mod Organizer 2 instance", out)

    def test_use_then_status(self):
        inst = self.tmp / "MO2"
        inst.mkdir()
        code, out = self.run_cli("mo2", "use", str(inst))
        self.assertEqual(code, 0, out)
        self.assertEqual(State.load().instance_path, str(inst))
        code, out = self.run_cli("mo2", "status")
        self.assertEqual(code, 0)
        self.assertIn("Sync:      off", out)

    def test_use_rejects_missing_dir(self):
        code, out = self.run_cli("mo2", "use", str(self.tmp / "nope"))
        self.assertEqual(code, 1)
        self.assertIn("Not a directory", out)

    def test_sync_and_vault_are_the_same_command(self):
        for name in ("sync", "vault"):
            code, out = self.run_cli(name)
            self.assertEqual(code, 2)
            self.assertIn("modsync sync create", out)


if __name__ == "__main__":
    unittest.main()
