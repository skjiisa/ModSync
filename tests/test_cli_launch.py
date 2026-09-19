"""`modsync launch` wires the hook into the CLI."""

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import launchhook
from modsync.__main__ import main
from modsync.steam import libraries as libs
from modsync.steam import shortcuts
from tests import fakesteam


class LaunchCommandTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "config")
        self.addCleanup(os.environ.pop, "XDG_CONFIG_HOME", None)
        root = fakesteam.make_steam(self.tmp / "Steam", mapping={489830: {"name": "GE-Proton10-34", "config": "", "priority": "250"}})
        env = launchhook.SteamEnv(root, libs.all_libraries([root]))
        for p in (
            patch.object(launchhook, "steam_env", lambda: env),
            patch.object(shortcuts, "steam_is_running", lambda: False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(list(args))
        return code, out.getvalue()

    def test_usage(self):
        code, out = self.run_cli("launch")
        self.assertEqual(code, 2)
        self.assertIn("modsync launch enable", out)

    def test_status_enable_disable(self):
        code, out = self.run_cli("launch", "status")
        self.assertEqual(code, 1)
        self.assertIn("Launch hook:    off", out)
        code, out = self.run_cli("launch", "enable")
        self.assertEqual(code, 0, out)
        self.assertIn("continues to the game (GE-Proton10-34)", out)
        code, out = self.run_cli("launch", "status")
        self.assertEqual(code, 0)
        self.assertIn("Launch hook:    on", out)
        self.assertIn("Steam runs Skyrim Special Edition with: modsync_489830_hub", out)
        code, out = self.run_cli("launch", "disable")
        self.assertEqual(code, 0)
        self.assertIn("back to GE-Proton10-34", out)

    def test_enable_bad_flag_and_unknown_tool(self):
        self.assertEqual(self.run_cli("launch", "enable", "--bogus")[0], 2)
        code, out = self.run_cli("launch", "enable", "--through", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Cannot enable", out)

    def test_help_mentions_launch(self):
        code, out = self.run_cli("--help")
        self.assertIn("modsync launch status", out)


if __name__ == "__main__":
    unittest.main()
