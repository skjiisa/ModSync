"""`modsync launch` wires the hook into the CLI."""

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from modsync import cli, launchhook
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
        self.assertIn("Steam runs Skyrim Special Edition with: modsync_489830_proton", out)
        code, out = self.run_cli("launch", "disable")
        self.assertEqual(code, 0)
        self.assertIn("back to GE-Proton10-34", out)

    def test_enable_bad_flag_and_unknown_tool(self):
        self.assertEqual(self.run_cli("launch", "enable", "--bogus")[0], 2)
        code, out = self.run_cli("launch", "enable", "--through", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Cannot enable", out)

    def test_play_mode(self):
        code, out = self.run_cli("launch", "mode")
        self.assertEqual(code, 0)
        self.assertIn("Steam's Play: countdown.", out)
        code, out = self.run_cli("launch", "mode", "wait")
        self.assertEqual((code, launchhook.play_mode()), (0, "wait"))
        self.assertIn("waits for you to choose", out)
        code, out = self.run_cli("launch", "mode", "later")
        self.assertEqual((code, launchhook.play_mode()), (2, "wait"))
        self.assertIn("unknown play mode", out)

    def test_help_mentions_launch(self):
        code, out = self.run_cli("--help")
        self.assertIn("modsync launch status", out)


class HubOutputTests(unittest.TestCase):
    """The hub's stdout carries only the handoff; anything else said goes to stderr."""

    def test_stdout_is_reserved_for_the_handoff(self):
        script = (
            "import sys\n"
            "import modsync.ui.app as app\n"
            "from modsync import cli\n"
            "def fake(*, appid, through, handoff, reason):\n"
            "    print('a stray print')\n"
            "    handoff.write(b'the-handoff\\0')\n"
            "    return 0\n"
            "app.run_hub = fake\n"
            "sys.exit(cli._launch_hub(['--appid', '489830', '--through', 'proton_experimental']))\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=60,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"the-handoff\0")
        self.assertIn(b"a stray print", result.stderr)


class PlayRightAwayTests(unittest.TestCase):
    """Steam's Play set to "play": no window while nothing needs attention."""

    def setUp(self):
        from modsync.mo2.launch import LaunchPlan
        from modsync.state import State

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "config"),
                                          "XDG_STATE_HOME": str(self.tmp / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MODSYNC_HUB_AUTO_DECISION", None)
        launchhook.set_play_mode("play")
        State(instance_path=str(self.tmp)).save()
        command = [str(self.tmp / "ModOrganizer.exe"), "run", "-e", "SKSE"]
        self.plan = LaunchPlan(["proton", "run", *command], {}, self.tmp, "SKSE", True, self.tmp / "Proton", command)

    def hub(self, attention=None, prepare=None):
        from modsync import cli
        from modsync.service import ModSyncService

        handoff = io.BytesIO()
        with patch.object(ModSyncService, "play_attention", return_value=attention), \
                patch.object(ModSyncService, "prepare_mo2", side_effect=prepare, return_value=(self.plan, "")), \
                patch("modsync.ui.app.run_hub", return_value=launchhook.EXIT_CANCEL) as run_hub, \
                patch.object(os, "fdopen", return_value=handoff), patch.object(os, "dup2"):
            handoff.close = lambda: None
            code = cli._launch_hub(["--appid", "489830", "--through", "proton_experimental"])
        return code, handoff.getvalue(), run_hub

    def test_plays_without_the_window(self):
        code, written, run_hub = self.hub()
        self.assertEqual(code, launchhook.EXIT_CONTINUE)
        self.assertEqual(written, launchhook.encode_handoff(self.plan))
        run_hub.assert_not_called()

    def test_asks_a_running_syncthing_about_changes_still_arriving(self):
        from modsync.service import ModSyncService
        from modsync.state import State
        from modsync.sync.manager import SyncthingManager

        State(instance_path=str(self.tmp), folder_id="modsync-1").save()
        seen = []
        with patch.object(SyncthingManager, "attach", return_value=True) as attach, \
                patch.object(ModSyncService, "play_attention",
                             lambda svc: seen.append(svc.manager) or "3 changes haven't arrived yet."):
            handoff = io.BytesIO()
            with patch("modsync.ui.app.run_hub", return_value=launchhook.EXIT_CANCEL) as run_hub, \
                    patch.object(os, "fdopen", return_value=handoff), patch.object(os, "dup2"):
                handoff.close = lambda: None
                cli._launch_hub(["--appid", "489830"])
        attach.assert_called_once()
        self.assertEqual(run_hub.call_args.kwargs["reason"], "3 changes haven't arrived yet.")

    def test_something_to_decide_opens_the_window_and_says_why(self):
        code, written, run_hub = self.hub(attention="SKSE does not match the installed game.")
        self.assertEqual((code, written), (launchhook.EXIT_CANCEL, b""))
        self.assertEqual(run_hub.call_args.kwargs["reason"], "SKSE does not match the installed game.")

    def test_a_launch_it_cannot_prepare_opens_the_window(self):
        code, written, run_hub = self.hub(prepare=RuntimeError("MO2 hasn't been set up for Skyrim yet."))
        self.assertEqual(written, b"")
        self.assertIn("MO2 hasn't been set up", run_hub.call_args.kwargs["reason"])

    def test_other_modes_open_the_window(self):
        launchhook.set_play_mode("countdown")
        code, written, run_hub = self.hub()
        run_hub.assert_called_once()
        self.assertEqual(run_hub.call_args.kwargs["reason"], "")


if __name__ == "__main__":
    unittest.main()


def test_help_flag_anywhere_prints_usage(capsys):
    from modsync.__main__ import main

    for argv in (["--help"], ["serve", "--help"], ["game", "-h"], ["launch", "enable", "--help"]):
        assert main(argv) == 0
        assert "usage:" in capsys.readouterr().out
