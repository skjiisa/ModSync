"""The state file: its version guard."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from modsync.__main__ import main
from modsync.state import EXIT_NEWER_STATE, SCHEMA, NewerStateError, State


class Schema(unittest.TestCase):
    """Every copy of ModSync on a machine shares the state file, so an older one
    must not misread, or write over, one a newer ModSync has changed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_its_version_is_written(self):
        State.update(dfu_path="/games/dfu")
        self.assertEqual(json.loads(State.path().read_text())["schema"], SCHEMA)

    def test_a_file_from_before_versions_loads(self):
        State.path().parent.mkdir(parents=True, exist_ok=True)
        State.path().write_text(json.dumps({"dfu_path": "/games/dfu"}))
        self.assertEqual(State.load().dfu_path, "/games/dfu")

    def test_a_newer_ones_file_is_left_alone(self):
        State.path().parent.mkdir(parents=True, exist_ok=True)
        newer = json.dumps({"dfu_path": "/games/dfu", "schema": SCHEMA + 1, "something_new": [1]})
        State.path().write_text(newer)
        with self.assertRaisesRegex(NewerStateError, "newer ModSync"):
            State.load()
        with self.assertRaises(NewerStateError):
            State.update(dfu_path="/elsewhere")
        with self.assertRaises(NewerStateError):
            State().save()  # a whole reset too
        self.assertEqual(State.path().read_text(), newer)

    def test_the_command_line_says_so(self):
        State.path().parent.mkdir(parents=True, exist_ok=True)
        State.path().write_text(json.dumps({"schema": SCHEMA + 1}))
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), \
                patch("modsync.__main__._show_error") as window:
            self.assertEqual(main(["dfu", "status"]), EXIT_NEWER_STATE)
        self.assertIn("is from a newer ModSync", err.getvalue())
        window.assert_not_called()

    def test_the_app_says_so_in_a_window(self):
        State.path().parent.mkdir(parents=True, exist_ok=True)
        State.path().write_text(json.dumps({"schema": SCHEMA + 1}))
        with redirect_stderr(io.StringIO()), patch("modsync.cli.launch_gui", side_effect=lambda a: State.load()), \
                patch("modsync.__main__._show_error") as window:
            self.assertEqual(main([]), EXIT_NEWER_STATE)
        self.assertIn("is from a newer ModSync", window.call_args.args[0])

