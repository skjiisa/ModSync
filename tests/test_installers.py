import stat
import tempfile
import textwrap
import unittest
from pathlib import Path

from modsync.games import SKYRIM_SE
from modsync.mo2.installers.mo2lint import Mo2LintBackend

FAKE_OK = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys, pathlib
    print("fake mo2-lint args:", sys.argv[1:])
    dest = pathlib.Path(sys.argv[3])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "ModOrganizer.exe").write_bytes(b"MZ")
    (dest / "mods").mkdir(exist_ok=True)
    sys.exit(0)
    """
)

FAKE_FAIL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import sys
    print("boom", file=sys.stderr)
    sys.exit(3)
    """
)


def _script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class Mo2LintBackendTests(unittest.TestCase):
    def test_install_success_builds_command_and_detects_exe(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_OK)
            dest = tmp / "MO2"
            lines: list[str] = []

            result = Mo2LintBackend(binary=fake).install(
                SKYRIM_SE, dest, script_extender=True, on_output=lines.append
            )

            self.assertTrue(result.success, result.message)
            self.assertEqual(result.instance_path, dest)
            self.assertTrue((dest / "ModOrganizer.exe").exists())
            joined = "\n".join(lines)
            self.assertIn("skyrim_se", joined)
            self.assertIn("--unattended", joined)
            self.assertIn("--script-extender", joined)

    def test_install_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            fake = _script(tmp / "fake-mo2-lint", FAKE_FAIL)
            result = Mo2LintBackend(binary=fake).install(
                SKYRIM_SE, tmp / "MO2", on_output=lambda _line: None
            )
            self.assertFalse(result.success)
            self.assertEqual(result.returncode, 3)
            self.assertIsNone(result.instance_path)


if __name__ == "__main__":
    unittest.main()
